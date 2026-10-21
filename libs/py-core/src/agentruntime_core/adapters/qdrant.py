"""VectorStore 的 Qdrant 适配器（设计书 4.4、附录 I.6、I.7；计划 S-12b）。语义见 agentruntime_core.providers.vector_store。

- 多租户布局：每个（语料、embedding_version）一个集合，单集合按租户字段分区：payload 的 tenant_id 建 keyword 索引
  （is_tenant）。每次读写、查询、计数、删除都由适配器拼上 tenant_id 过滤，调用方给不出也改不了。
  平台作用域放在独立的集合里（名字以 __p 结尾），与任何租户互不可见。集合名里的版本是散列，embedding_version
  原文存在每个点的 _version 里。
- 点的 id 由（作用域、语料、记录 id）派生的 UUID，不同租户的同一个记录 id 互不覆盖；原 id 存在 payload 的 _id。
- 向量按租户密钥做保距变换后再入库（vector_crypto）：稠密向量是正交变换，稀疏向量是下标置换；密钥来自 KeySource
  （用途 vector_transform）。取回时再变换回来。集合用余弦距离，原向量的范数存在 payload 的 _norm。
- 稠密 + 稀疏的混合查询在客户端用 RRF（k = 60）融合，与开发实现一致。
- 字段名 tenant_id、scope 是保留字；以 _ 开头的字段名也留给适配器自己用。
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from agentruntime_core.adapters.keyring import KeySource
from agentruntime_core.adapters.vector_crypto import DenseTransform, SparseTransform
from agentruntime_core.dev._common import scope_id
from agentruntime_core.errors import InvalidError, ProviderError, TransientError
from agentruntime_core.providers.vector_store import AnyOf, FieldValue, Filter, Hit, SparseVector, VectorRecord
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector

TRANSFORM_PURPOSE = "vector_transform"
TENANT_FIELD = "tenant_id"
RRF_K = 60
DENSE = "dense"
SPARSE = "sparse"
_CORPUS = re.compile(r"[a-z][a-z0-9_]{0,62}")
_RESERVED = {"tenant_id", "scope"}
_NAMESPACE = uuid.UUID("8f5a1f0e-6b1c-4f3e-9a54-5b7f2b0f9c11")
_PLATFORM_SUFFIX = "__p"


def _check_corpus(corpus: str) -> None:
    if not isinstance(corpus, str) or not _CORPUS.fullmatch(corpus):
        raise InvalidError("invalid_corpus", "语料名必须是小写字母开头的 1 到 63 个小写字母、数字或下划线")


def _check_fields(fields: Mapping[str, Any]) -> None:
    for key in fields:
        if key in _RESERVED or key.startswith("_"):
            raise InvalidError("reserved_field", "tenant_id、scope 与以 _ 开头的字段名是保留字段")


def _version_slug(version: str) -> str:
    """集合名里的版本部分：散列，保持集合名短（Windows 上 Qdrant 的存储路径受 260 字符限制）。"""
    return "v" + hashlib.sha256(version.encode()).hexdigest()[:12]


def _map_error(error: Exception) -> ProviderError:
    if isinstance(error, UnexpectedResponse) and error.status_code is not None and 400 <= error.status_code < 500:
        return InvalidError("bad_request", f"Qdrant 拒绝了请求（{error.status_code}）")
    return TransientError("unavailable", "向量库暂时不可用")


def _from_payload(value: Any) -> FieldValue:
    return tuple(value) if isinstance(value, list) else value


def _to_payload(value: FieldValue) -> Any:
    return list(value) if isinstance(value, (tuple, list)) else value


class QdrantVectorStoreProvider:
    def __init__(self, *, url: str, keys: KeySource, prefix: str = "agentruntime", faults: FaultInjector | None = None, timeout_s: float = 10) -> None:
        self.url = url
        self.keys = keys
        self.prefix = prefix
        self.faults = faults or FaultInjector()
        self.timeout_s = timeout_s
        self._client = AsyncQdrantClient(url=url, timeout=int(timeout_s), check_compatibility=False)
        self._collections: dict[str, int] = {}

    def bind(self, tenant: TenantContext) -> QdrantVectorStore:
        return QdrantVectorStore(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> QdrantVectorStore:
        return QdrantVectorStore(self, platform_scope(purpose))

    def collection_name(self, corpus: str, embedding_version: str, *, platform: bool = False) -> str:
        """测试与运维用：语料与 embedding_version 对应的集合名。"""
        name = f"{self.prefix}__{corpus}__{_version_slug(embedding_version)}"
        return name + _PLATFORM_SUFFIX if platform else name

    async def aclose(self) -> None:
        await self._client.close()

    # --- 集合 ---

    async def _list(self, corpus: str, *, platform: bool) -> list[str]:
        """语料在各个 embedding_version 下的集合。"""
        response = await self._client.get_collections()
        start = f"{self.prefix}__{corpus}__"
        found = []
        for c in response.collections:
            if not c.name.startswith(start):
                continue
            rest = c.name[len(start) :]
            if platform:
                if not rest.endswith(_PLATFORM_SUFFIX):
                    continue
                rest = rest[: -len(_PLATFORM_SUFFIX)]
            if rest and "__" not in rest:  # 别的语料（名字里带 __）或别的作用域的集合不算
                found.append(c.name)
        return sorted(found)

    async def _dims(self, name: str) -> int | None:
        """集合的稠密向量维度；集合不存在时返回 None。"""
        if name in self._collections:
            return self._collections[name]
        try:
            info = await self._client.get_collection(name)
        except UnexpectedResponse as error:
            if error.status_code == 404:
                return None
            raise
        params = info.config.params.vectors
        size = params[DENSE].size if isinstance(params, dict) else params.size  # type: ignore[union-attr]
        self._collections[name] = int(size)
        return int(size)

    async def _ensure(self, name: str, dims: int) -> None:
        existing = await self._dims(name)
        if existing is not None:
            if existing != dims:
                raise InvalidError("dimension_mismatch", "同一语料、同一 embedding_version 下的向量维度必须一致")
            return
        try:
            await self._client.create_collection(
                name,
                vectors_config={DENSE: models.VectorParams(size=dims, distance=models.Distance.COSINE)},
                sparse_vectors_config={SPARSE: models.SparseVectorParams()},
            )
        except UnexpectedResponse:
            if await self._dims(name) is None:  # 并发创建时别人先建好了；否则是真的失败
                raise
        await self._client.create_payload_index(
            name, field_name=TENANT_FIELD, field_schema=models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD, is_tenant=True), wait=True
        )
        self._collections.pop(name, None)
        dims_now = await self._dims(name)
        if dims_now != dims:
            raise InvalidError("dimension_mismatch", "同一语料、同一 embedding_version 下的向量维度必须一致")


class QdrantVectorStore:
    def __init__(self, provider: QdrantVectorStoreProvider, scope: Scope) -> None:
        self._p = provider
        self._scope = scope
        self._owner = scope_id(scope)
        self._dense: dict[int, DenseTransform] = {}
        self._sparse: SparseTransform | None = None

    async def _key(self) -> bytes:
        return await self._p.keys.key(self._scope, TRANSFORM_PURPOSE)

    async def _dense_transform(self, dims: int) -> DenseTransform:
        t = self._dense.get(dims)
        if t is None:
            t = self._dense[dims] = DenseTransform(await self._key(), dims)
        return t

    async def _sparse_transform(self) -> SparseTransform:
        if self._sparse is None:
            self._sparse = SparseTransform(await self._key())
        return self._sparse

    def _point_id(self, corpus: str, record_id: str) -> str:
        return str(uuid.uuid5(_NAMESPACE, f"{self._owner}\x00{corpus}\x00{record_id}"))

    def _name(self, corpus: str, version: str) -> str:
        return self._p.collection_name(corpus, version, platform=self._scope.is_platform)

    def _tenant(self) -> models.FieldCondition:
        return models.FieldCondition(key=TENANT_FIELD, match=models.MatchValue(value=self._owner))

    def _filter(self, flt: Filter | None, *extra: models.Condition) -> models.Filter:
        must: list[models.Condition] = [self._tenant(), *extra]
        for key, expected in (flt.must.items() if flt else []):
            if isinstance(expected, AnyOf):
                must.append(models.FieldCondition(key=key, match=models.MatchAny(any=list(expected.values))))
            elif isinstance(expected, (tuple, list)):
                must.extend(models.FieldCondition(key=key, match=models.MatchValue(value=v)) for v in expected)
            elif isinstance(expected, float):
                must.append(models.FieldCondition(key=key, range=models.Range(gte=expected, lte=expected)))
            else:
                must.append(models.FieldCondition(key=key, match=models.MatchValue(value=expected)))
        return models.Filter(must=must)

    async def _collections(self, corpus: str) -> list[str]:
        return await self._p._list(corpus, platform=self._scope.is_platform)

    async def _call(self, coro: Any) -> Any:
        try:
            return await coro
        except ProviderError:
            raise
        except (UnexpectedResponse, ResponseHandlingException, httpx.HTTPError, OSError) as error:
            raise _map_error(error) from error

    # --- 读写 ---

    async def upsert(self, corpus: str, records: Sequence[VectorRecord]) -> None:
        self._p.faults.check("upsert")
        _check_corpus(corpus)
        by_version: dict[str, list[models.PointStruct]] = {}
        dims_of: dict[str, int] = {}
        sparse_t = await self._sparse_transform()
        for record in records:
            if not record.id:
                raise InvalidError("invalid_argument", "记录必须有 id")
            _check_fields(record.fields)
            dims = len(record.dense)
            if dims_of.setdefault(record.embedding_version, dims) != dims:
                raise InvalidError("dimension_mismatch", "同一语料、同一 embedding_version 下的向量维度必须一致")
            dense = tuple(float(x) for x in record.dense)
            norm = math.sqrt(sum(x * x for x in dense))
            vector: dict[str, Any] = {DENSE: list((await self._dense_transform(dims)).forward(dense))}
            if record.sparse is not None:
                s = sparse_t.forward(record.sparse)
                vector[SPARSE] = models.SparseVector(indices=list(s.indices), values=list(s.values))
            payload = {k: _to_payload(v) for k, v in record.fields.items()}
            payload.update({TENANT_FIELD: self._owner, "_id": record.id, "_norm": norm, "_version": record.embedding_version})
            by_version.setdefault(record.embedding_version, []).append(models.PointStruct(id=self._point_id(corpus, record.id), vector=vector, payload=payload))
        for version, points in by_version.items():
            await self._call(self._p._ensure(self._name(corpus, version), dims_of[version]))
        existing = await self._call(self._collections(corpus))
        for version, points in by_version.items():
            target = self._name(corpus, version)
            ids = [p.id for p in points]
            # 同一个 id 换了 embedding_version：先从别的版本里删掉，保持“按 id 覆盖”。
            for other in existing:
                if other != target:
                    await self._call(self._p._client.delete(other, points_selector=models.FilterSelector(filter=self._filter(None, models.HasIdCondition(has_id=ids))), wait=True))
            await self._call(self._p._client.upsert(target, points=points, wait=True))

    def _record(self, point: models.Record | models.ScoredPoint, version: str, dense_t: DenseTransform | None, sparse_t: SparseTransform) -> VectorRecord:
        payload = dict(point.payload or {})
        vector = point.vector if isinstance(point.vector, dict) else {}
        stored = vector.get(DENSE) or []
        norm = float(payload.get("_norm") or 0.0)
        dense: tuple[float, ...] = ()
        if dense_t is not None and stored:
            dense = dense_t.inverse([float(x) * norm for x in stored])  # type: ignore[union-attr]
        sparse = None
        raw_sparse = vector.get(SPARSE)
        if isinstance(raw_sparse, models.SparseVector):
            sparse = sparse_t.inverse(SparseVector(indices=tuple(raw_sparse.indices), values=tuple(raw_sparse.values)))
        fields = {k: _from_payload(v) for k, v in payload.items() if k != TENANT_FIELD and not k.startswith("_")}
        return VectorRecord(id=str(payload["_id"]), dense=dense, embedding_version=version, sparse=sparse, fields=fields)

    async def get(self, corpus: str, ids: Sequence[str]) -> list[VectorRecord]:
        self._p.faults.check("get")
        _check_corpus(corpus)
        point_ids = [self._point_id(corpus, i) for i in ids]
        found: dict[str, VectorRecord] = {}
        sparse_t = await self._sparse_transform()
        for name in await self._call(self._collections(corpus)):
            points = await self._call(self._p._client.retrieve(name, ids=point_ids, with_payload=True, with_vectors=True))
            dims = await self._call(self._p._dims(name))
            dense_t = await self._dense_transform(dims) if dims else None
            for point in points:
                payload = point.payload or {}
                if payload.get(TENANT_FIELD) != self._owner:
                    continue
                version = str(payload.get("_version", ""))
                found[str(payload["_id"])] = self._record(point, version, dense_t, sparse_t)
        return [found[i] for i in ids if i in found]

    async def delete(self, corpus: str, ids: Sequence[str]) -> int:
        self._p.faults.check("delete")
        _check_corpus(corpus)
        point_ids = sorted({self._point_id(corpus, i) for i in ids})
        if not point_ids:
            return 0
        removed = 0
        for name in await self._call(self._collections(corpus)):
            selector = self._filter(None, models.HasIdCondition(has_id=point_ids))
            counted = await self._call(self._p._client.count(name, count_filter=selector, exact=True))
            if counted.count:
                await self._call(self._p._client.delete(name, points_selector=models.FilterSelector(filter=selector), wait=True))
                removed += counted.count
        return removed

    async def query(
        self,
        corpus: str,
        *,
        embedding_version: str,
        top_k: int,
        dense: Sequence[float] | None = None,
        sparse: SparseVector | None = None,
        filter: Filter | None = None,
    ) -> list[Hit]:
        self._p.faults.check("query")
        _check_corpus(corpus)
        if top_k < 1:
            raise InvalidError("invalid_argument", "top_k 至少为 1")
        if dense is None and sparse is None:
            raise InvalidError("invalid_argument", "dense 与 sparse 至少给一个")
        if filter is not None:
            _check_fields(filter.must)
        name = self._name(corpus, embedding_version)
        dims = await self._call(self._p._dims(name))
        if dims is None:
            return []
        if dense is not None and dims != len(dense):
            raise InvalidError("dimension_mismatch", "查询向量的维度与语料不一致")
        flt = self._filter(filter)
        limit = top_k if dense is None or sparse is None else max(top_k * 4, 50)
        dense_hits: list[models.ScoredPoint] = []
        sparse_hits: list[models.ScoredPoint] = []
        if dense is not None:
            q = list((await self._dense_transform(dims)).forward([float(x) for x in dense]))
            response = await self._call(self._p._client.query_points(name, query=q, using=DENSE, query_filter=flt, limit=limit, with_payload=True))
            dense_hits = list(response.points)
        if sparse is not None:
            s = (await self._sparse_transform()).forward(sparse)
            if s.indices:
                response = await self._call(
                    self._p._client.query_points(
                        name, query=models.SparseVector(indices=list(s.indices), values=list(s.values)), using=SPARSE, query_filter=flt, limit=limit, with_payload=True
                    )
                )
                sparse_hits = [p for p in response.points if p.score > 0]

        def fields(point: models.ScoredPoint) -> dict[str, FieldValue]:
            return {k: _from_payload(v) for k, v in (point.payload or {}).items() if k != TENANT_FIELD and not k.startswith("_")}

        def rid(point: models.ScoredPoint) -> str:
            return str((point.payload or {})["_id"])

        if dense is not None and sparse is not None:
            scores: dict[str, float] = {}
            points: dict[str, models.ScoredPoint] = {}
            for ranking in (sorted(dense_hits, key=lambda p: (-p.score, rid(p))), sorted(sparse_hits, key=lambda p: (-p.score, rid(p)))):
                for rank, point in enumerate(ranking, start=1):
                    scores[rid(point)] = scores.get(rid(point), 0.0) + 1.0 / (RRF_K + rank)
                    points.setdefault(rid(point), point)
            ordered = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
            return [Hit(id=i, score=s, fields=fields(points[i])) for i, s in ordered[:top_k]]
        hits = dense_hits if dense is not None else sparse_hits
        hits = sorted(hits, key=lambda p: (-p.score, rid(p)))
        return [Hit(id=rid(p), score=float(p.score), fields=fields(p)) for p in hits[:top_k]]

    async def count(self, corpus: str, *, filter: Filter | None = None) -> int:
        self._p.faults.check("count")
        _check_corpus(corpus)
        if filter is not None:
            _check_fields(filter.must)
        total = 0
        for name in await self._call(self._collections(corpus)):
            result = await self._call(self._p._client.count(name, count_filter=self._filter(filter), exact=True))
            total += result.count
        return total
