"""VectorStore 的 Milvus 适配器（设计改动草稿 C1：默认改用 Milvus；设计书 4.4、附录 I.6、I.7）。语义见 agentruntime_core.providers.vector_store。

- 多租户布局：每个（语料、embedding_version）一个集合，租户字段 tenant_id 是集合的 partition key。每次读写、查询、计数、删除
  都由适配器拼上租户条件，调用方给不出也改不了。平台作用域放在独立的集合里（名字以 __p 结尾），与任何租户互不可见。
  集合名里的版本是散列，embedding_version 原文存在每行的 _version 里。
- 过滤表达式只由适配器拼接：值一律走模板参数（filter_params），不拼进字符串；字段名进不了模板，只接受普通标识符，
  在动态字段里用 $meta["名字"] 引用，避开表达式关键字。
- 主键 _pk 由（作用域、语料、记录 id）派生，不同租户的同一个记录 id 互不覆盖；原 id 存在 _id。调用方的其余字段放在动态字段里。
- 向量按租户密钥做保距变换后再入库（vector_crypto）：稠密向量是正交变换，稀疏向量是下标置换（下标空间收窄到
  2**32 - 1，Milvus 要求下标小于它）；密钥来自 KeySource（用途 vector_transform）。取回时再变换回来。
  稀疏向量由调用方算好传入，不用 Milvus 原生的 BM25 Function（要明文，与附录 I.7 冲突）。
- 集合用 Strong 一致性：upsert 之后马上读得到（Milvus 默认的 Bounded 做不到）。
- 稠密 + 稀疏的混合查询在客户端用 RRF（k = 60）融合，与开发实现一致。
- 连接是惰性的：建 Provider 时不连接，第一次调用才连；每个 Provider 用自己的连接（dedicated），不与别的事件循环共享。
- 字段名 tenant_id、scope 是保留字；以 _ 开头的字段名也留给适配器自己用。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from pymilvus import AsyncMilvusClient, DataType, MilvusClient
from pymilvus.exceptions import MilvusException, ParamError

from agentruntime_core.adapters.keyring import KeySource
from agentruntime_core.adapters.vector_crypto import DenseTransform, SparseTransform
from agentruntime_core.dev._common import scope_id
from agentruntime_core.errors import InvalidError, ProviderError, TransientError
from agentruntime_core.providers.vector_store import AnyOf, FieldValue, Filter, Hit, SparseVector, VectorRecord
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector

TRANSFORM_PURPOSE = "vector_transform"
TENANT_FIELD = "tenant_id"
PRIMARY_FIELD = "_pk"
RECORD_ID_FIELD = "_id"
VERSION_FIELD = "_version"
DENSE = "_dense"
SPARSE = "_sparse"
RRF_K = 60
SPARSE_INDEX_SPACE = 2**32 - 1
CONSISTENCY = "Strong"
_CORPUS = re.compile(r"[a-z][a-z0-9_]{0,62}")
_FIELD_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,254}")
_PREFIX = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_RESERVED = {"tenant_id", "scope"}
_NAMESPACE = uuid.UUID("3c7e9b52-1d0a-4f6e-8b21-6a9f0c4d2e57")
_PLATFORM_SUFFIX = "__p"
# Milvus 判为参数错误的状态码（例如维度不符）；其余错误按暂时不可用处理。
_INVALID_CODES = {1100}
_HNSW = {"M": 16, "efConstruction": 200}


def _check_corpus(corpus: str) -> None:
    if not isinstance(corpus, str) or not _CORPUS.fullmatch(corpus):
        raise InvalidError("invalid_corpus", "语料名必须是小写字母开头的 1 到 63 个小写字母、数字或下划线")


def _check_fields(fields: Mapping[str, Any]) -> None:
    for key in fields:
        if key in _RESERVED or key.startswith("_"):
            raise InvalidError("reserved_field", "tenant_id、scope 与以 _ 开头的字段名是保留字段")
        if not isinstance(key, str) or not _FIELD_NAME.fullmatch(key):
            raise InvalidError("invalid_field_name", "字段名只能是字母开头的字母、数字与下划线")


def _version_slug(version: str) -> str:
    """集合名里的版本部分：散列，保持集合名短，也不受 embedding_version 里的字符限制。"""
    return "v" + hashlib.sha256(version.encode()).hexdigest()[:12]


def _map_error(error: BaseException) -> ProviderError:
    if isinstance(error, ParamError) or (isinstance(error, MilvusException) and error.code in _INVALID_CODES):
        return InvalidError("bad_request", f"Milvus 拒绝了请求（{getattr(error, 'code', '?')}）")
    return TransientError("unavailable", "向量库暂时不可用")


def _from_meta(value: Any) -> FieldValue:
    return tuple(value) if isinstance(value, list) else value


def _to_meta(value: FieldValue) -> Any:
    return list(value) if isinstance(value, (tuple, list)) else value


def _user_fields(row: Mapping[str, Any]) -> dict[str, FieldValue]:
    """一行（或检索结果的 entity）里调用方的字段：去掉租户字段与适配器自己的 _ 字段。"""
    return {k: _from_meta(v) for k, v in row.items() if k != TENANT_FIELD and not k.startswith("_")}


class MilvusVectorStoreProvider:
    def __init__(
        self,
        *,
        uri: str,
        keys: KeySource,
        prefix: str = "agentruntime",
        token: str = "",
        faults: FaultInjector | None = None,
        timeout_s: float = 10,
    ) -> None:
        if not _PREFIX.fullmatch(prefix):
            raise InvalidError("invalid_prefix", "集合前缀只能是字母或下划线开头的字母、数字与下划线")
        self.uri = uri
        self.keys = keys
        self.prefix = prefix
        self.faults = faults or FaultInjector()
        self.timeout_s = timeout_s
        self._token = token
        self._slow_timeout = max(timeout_s, 60)  # 建集合、建索引、加载
        self._client: AsyncMilvusClient | None = None
        self._collections: dict[str, int] = {}

    def bind(self, tenant: TenantContext) -> MilvusVectorStore:
        return MilvusVectorStore(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> MilvusVectorStore:
        return MilvusVectorStore(self, platform_scope(purpose))

    def collection_name(self, corpus: str, embedding_version: str, *, platform: bool = False) -> str:
        """测试与运维用：语料与 embedding_version 对应的集合名。"""
        name = f"{self.prefix}__{corpus}__{_version_slug(embedding_version)}"
        return name + _PLATFORM_SUFFIX if platform else name

    def client(self) -> AsyncMilvusClient:
        if self._client is None:
            self._client = AsyncMilvusClient(uri=self.uri, token=self._token, timeout=self.timeout_s, dedicated=True)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            try:
                await client.close()
            except (MilvusException, OSError, asyncio.TimeoutError):
                pass  # 关闭时连不上，没有要释放的东西

    # --- 集合 ---

    async def _list(self, corpus: str, *, platform: bool) -> list[str]:
        """语料在各个 embedding_version 下的集合。"""
        start = f"{self.prefix}__{corpus}__"
        found = []
        for name in await self.client().list_collections(timeout=self.timeout_s):
            if not name.startswith(start):
                continue
            rest = name[len(start) :]
            if platform:
                if not rest.endswith(_PLATFORM_SUFFIX):
                    continue
                rest = rest[: -len(_PLATFORM_SUFFIX)]
            if rest and "__" not in rest:  # 别的语料（名字里带 __）或别的作用域的集合不算
                found.append(name)
        return sorted(found)

    async def _dims(self, name: str) -> int | None:
        """集合的稠密向量维度；集合不存在时返回 None。第一次见到已有的集合时确保它已加载。"""
        if name in self._collections:
            return self._collections[name]
        client = self.client()
        if not await client.has_collection(name, timeout=self.timeout_s):
            return None
        info = await client.describe_collection(name, timeout=self.timeout_s)
        dims = next(int(f["params"]["dim"]) for f in info["fields"] if f["name"] == DENSE)
        await client.load_collection(name, timeout=self._slow_timeout)  # 已加载时什么也不做；Milvus 重启后可能需要
        self._collections[name] = dims
        return dims

    async def _ensure(self, name: str, dims: int) -> None:
        existing = await self._dims(name)
        if existing is not None:
            if existing != dims:
                raise InvalidError("dimension_mismatch", "同一语料、同一 embedding_version 下的向量维度必须一致")
            return
        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field(PRIMARY_FIELD, DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field(TENANT_FIELD, DataType.VARCHAR, max_length=256, is_partition_key=True)
        schema.add_field(RECORD_ID_FIELD, DataType.VARCHAR, max_length=1024)
        schema.add_field(VERSION_FIELD, DataType.VARCHAR, max_length=256)
        schema.add_field(DENSE, DataType.FLOAT_VECTOR, dim=dims)
        schema.add_field(SPARSE, DataType.SPARSE_FLOAT_VECTOR)
        index = MilvusClient.prepare_index_params()
        index.add_index(DENSE, index_type="HNSW", metric_type="COSINE", params=_HNSW)
        index.add_index(SPARSE, index_type="SPARSE_INVERTED_INDEX", metric_type="IP")
        try:
            await self.client().create_collection(name, schema=schema, index_params=index, consistency_level=CONSISTENCY, timeout=self._slow_timeout)
        except MilvusException:
            if await self._dims(name) is None:  # 并发创建时别人先建好了；否则是真的失败
                raise
        dims_now = await self._dims(name)
        if dims_now != dims:
            raise InvalidError("dimension_mismatch", "同一语料、同一 embedding_version 下的向量维度必须一致")


class MilvusVectorStore:
    def __init__(self, provider: MilvusVectorStoreProvider, scope: Scope) -> None:
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
            self._sparse = SparseTransform(await self._key(), index_space=SPARSE_INDEX_SPACE)
        return self._sparse

    async def _sparse_forward(self, vector: SparseVector) -> dict[int, float]:
        try:
            s = (await self._sparse_transform()).forward(vector)
        except ValueError as error:
            raise InvalidError("invalid_sparse_index", f"稀疏下标必须在 0 到 {SPARSE_INDEX_SPACE - 1} 之间") from error
        return dict(zip(s.indices, s.values, strict=True))

    def _pk(self, corpus: str, record_id: str) -> str:
        return str(uuid.uuid5(_NAMESPACE, f"{self._owner}\x00{corpus}\x00{record_id}"))

    def _name(self, corpus: str, version: str) -> str:
        return self._p.collection_name(corpus, version, platform=self._scope.is_platform)

    def _expr(self, flt: Filter | None, *, pks: Sequence[str] | None = None) -> tuple[str, dict[str, Any]]:
        """租户条件加调用方的条件；值都放进模板参数。"""
        parts = [f"{TENANT_FIELD} == {{_t}}"]
        params: dict[str, Any] = {"_t": self._owner}
        if pks is not None:
            parts.append(f"{PRIMARY_FIELD} in {{_pks}}")
            params["_pks"] = list(pks)
        for n, (key, expected) in enumerate(flt.must.items() if flt else []):
            field, p = f'$meta["{key}"]', f"p{n}"
            if isinstance(expected, AnyOf):
                parts.append(f"(({field} in {{{p}}}) or json_contains_any({field}, {{{p}}}))")
                params[p] = list(expected.values)
            else:
                parts.append(f"{field} == {{{p}}}")
                params[p] = _to_meta(expected)
        return " and ".join(parts), params

    async def _collections(self, corpus: str) -> list[str]:
        return await self._p._list(corpus, platform=self._scope.is_platform)

    async def _call(self, coro: Any) -> Any:
        try:
            return await coro
        except ProviderError:
            raise
        except (MilvusException, OSError, asyncio.TimeoutError) as error:
            raise _map_error(error) from error

    # --- 读写 ---

    async def upsert(self, corpus: str, records: Sequence[VectorRecord]) -> None:
        self._p.faults.check("upsert")
        _check_corpus(corpus)
        by_version: dict[str, list[dict[str, Any]]] = {}
        dims_of: dict[str, int] = {}
        for record in records:
            if not record.id:
                raise InvalidError("invalid_argument", "记录必须有 id")
            _check_fields(record.fields)
            dims = len(record.dense)
            if dims_of.setdefault(record.embedding_version, dims) != dims:
                raise InvalidError("dimension_mismatch", "同一语料、同一 embedding_version 下的向量维度必须一致")
            row: dict[str, Any] = {k: _to_meta(v) for k, v in record.fields.items()}
            row.update(
                {
                    PRIMARY_FIELD: self._pk(corpus, record.id),
                    TENANT_FIELD: self._owner,
                    RECORD_ID_FIELD: record.id,
                    VERSION_FIELD: record.embedding_version,
                    DENSE: list((await self._dense_transform(dims)).forward([float(x) for x in record.dense])),
                    SPARSE: await self._sparse_forward(record.sparse) if record.sparse is not None else {},
                }
            )
            by_version.setdefault(record.embedding_version, []).append(row)
        for version in by_version:
            await self._call(self._p._ensure(self._name(corpus, version), dims_of[version]))
        existing = await self._call(self._collections(corpus))
        client = self._p.client()
        for version, rows in by_version.items():
            target = self._name(corpus, version)
            # 同一个 id 换了 embedding_version：先从别的版本里删掉，保持“按 id 覆盖”。
            expr, params = self._expr(None, pks=[r[PRIMARY_FIELD] for r in rows])
            for other in existing:
                if other != target:
                    await self._call(client.delete(other, filter=expr, filter_params=params, timeout=self._p.timeout_s))
            await self._call(client.upsert(target, rows, timeout=self._p.timeout_s))

    def _record(self, row: Mapping[str, Any], dense_t: DenseTransform | None, sparse_t: SparseTransform) -> VectorRecord:
        dense: tuple[float, ...] = ()
        stored = row.get(DENSE) or []
        if dense_t is not None and len(stored):
            dense = dense_t.inverse([float(x) for x in stored])
        raw_sparse = dict(row.get(SPARSE) or {})
        sparse = None
        if raw_sparse:
            items = sorted((int(i), float(v)) for i, v in raw_sparse.items())
            sparse = sparse_t.inverse(SparseVector(indices=tuple(i for i, _ in items), values=tuple(v for _, v in items)))
        return VectorRecord(id=str(row[RECORD_ID_FIELD]), dense=dense, embedding_version=str(row.get(VERSION_FIELD, "")), sparse=sparse, fields=_user_fields(row))

    async def get(self, corpus: str, ids: Sequence[str]) -> list[VectorRecord]:
        self._p.faults.check("get")
        _check_corpus(corpus)
        if not ids:
            return []
        expr, params = self._expr(None, pks=[self._pk(corpus, i) for i in ids])
        found: dict[str, VectorRecord] = {}
        sparse_t = await self._sparse_transform()
        client = self._p.client()
        for name in await self._call(self._collections(corpus)):
            dims = await self._call(self._p._dims(name))
            rows = await self._call(client.query(name, filter=expr, filter_params=params, output_fields=["*"], consistency_level=CONSISTENCY, timeout=self._p.timeout_s))
            dense_t = await self._dense_transform(dims) if dims else None
            for row in rows:
                found[str(row[RECORD_ID_FIELD])] = self._record(row, dense_t, sparse_t)
        return [found[i] for i in ids if i in found]

    async def delete(self, corpus: str, ids: Sequence[str]) -> int:
        self._p.faults.check("delete")
        _check_corpus(corpus)
        pks = sorted({self._pk(corpus, i) for i in ids})
        if not pks:
            return 0
        expr, params = self._expr(None, pks=pks)
        removed = 0
        client = self._p.client()
        for name in await self._call(self._collections(corpus)):
            result = await self._call(client.delete(name, filter=expr, filter_params=params, timeout=self._p.timeout_s))
            removed += int(result.get("delete_count", 0)) if isinstance(result, Mapping) else 0
        return removed

    async def _search(self, name: str, *, data: Any, field: str, metric: str, params: dict[str, Any], limit: int, expr: str, values: dict[str, Any]) -> list[dict[str, Any]]:
        result = await self._call(
            self._p.client().search(
                name,
                data=[data],
                anns_field=field,
                limit=limit,
                filter=expr,
                filter_params=values,
                output_fields=[RECORD_ID_FIELD, "$meta"],
                search_params={"metric_type": metric, "params": params},
                consistency_level=CONSISTENCY,
                timeout=self._p.timeout_s,
            )
        )
        return list(result[0]) if result else []

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
        expr, values = self._expr(filter)
        limit = top_k if dense is None or sparse is None else max(top_k * 4, 50)
        dense_hits: list[dict[str, Any]] = []
        sparse_hits: list[dict[str, Any]] = []
        if dense is not None:
            q = list((await self._dense_transform(dims)).forward([float(x) for x in dense]))
            dense_hits = await self._search(name, data=q, field=DENSE, metric="COSINE", params={"ef": max(limit, 64)}, limit=limit, expr=expr, values=values)
        if sparse is not None:
            s = await self._sparse_forward(sparse)
            if s:
                hits = await self._search(name, data=s, field=SPARSE, metric="IP", params={"drop_ratio_search": 0.0}, limit=limit, expr=expr, values=values)
                sparse_hits = [h for h in hits if float(h["distance"]) > 0]

        def rid(hit: Mapping[str, Any]) -> str:
            return str(hit["entity"][RECORD_ID_FIELD])

        def fields(hit: Mapping[str, Any]) -> dict[str, FieldValue]:
            return _user_fields(hit["entity"])

        if dense is not None and sparse is not None:
            scores: dict[str, float] = {}
            first: dict[str, Mapping[str, Any]] = {}
            for ranking in (sorted(dense_hits, key=lambda h: (-float(h["distance"]), rid(h))), sorted(sparse_hits, key=lambda h: (-float(h["distance"]), rid(h)))):
                for rank, hit in enumerate(ranking, start=1):
                    scores[rid(hit)] = scores.get(rid(hit), 0.0) + 1.0 / (RRF_K + rank)
                    first.setdefault(rid(hit), hit)
            ordered = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
            return [Hit(id=i, score=s, fields=fields(first[i])) for i, s in ordered[:top_k]]
        hits = sorted(dense_hits if dense is not None else sparse_hits, key=lambda h: (-float(h["distance"]), rid(h)))
        return [Hit(id=rid(h), score=float(h["distance"]), fields=fields(h)) for h in hits[:top_k]]

    async def count(self, corpus: str, *, filter: Filter | None = None) -> int:
        self._p.faults.check("count")
        _check_corpus(corpus)
        if filter is not None:
            _check_fields(filter.must)
        expr, params = self._expr(filter)
        total = 0
        client = self._p.client()
        for name in await self._call(self._collections(corpus)):
            rows = await self._call(client.query(name, filter=expr, filter_params=params, output_fields=["count(*)"], consistency_level=CONSISTENCY, timeout=self._p.timeout_s))
            total += int(rows[0]["count(*)"]) if rows else 0
        return total
