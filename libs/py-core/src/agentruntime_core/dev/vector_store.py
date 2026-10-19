"""内存 VectorStore（开发实现，精确检索）。语义见 agentruntime_core.providers.vector_store。"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import replace

from agentruntime_core.dev._common import scope_id
from agentruntime_core.errors import InvalidError
from agentruntime_core.providers.vector_store import AnyOf, FieldValue, Filter, Hit, SparseVector, VectorRecord
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector

_CORPUS = re.compile(r"[a-z][a-z0-9_]{0,62}")
_RESERVED = {"tenant_id", "scope"}
RRF_K = 60


def _check_corpus(corpus: str) -> None:
    if not isinstance(corpus, str) or not _CORPUS.fullmatch(corpus):
        raise InvalidError("invalid_corpus", "语料名必须是小写字母开头的 1 到 63 个小写字母、数字或下划线")


def _check_fields(fields: object) -> None:
    keys = set(fields) if isinstance(fields, dict) or hasattr(fields, "keys") else set()
    if keys & _RESERVED:
        raise InvalidError("reserved_field", "tenant_id 与 scope 是保留字段")


def _normalize(value: FieldValue) -> FieldValue:
    return tuple(value) if isinstance(value, list) else value


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _dot(a: SparseVector, b: SparseVector) -> float:
    weights = dict(zip(a.indices, a.values, strict=True))
    return sum(weights.get(i, 0.0) * v for i, v in zip(b.indices, b.values, strict=True))


def _matches(fields: dict[str, FieldValue], flt: Filter | None) -> bool:
    if flt is None:
        return True
    for key, expected in flt.must.items():
        actual = fields.get(key)
        if isinstance(expected, AnyOf):
            options = set(expected.values)
            if isinstance(actual, tuple):
                if not options & set(actual):
                    return False
            elif actual not in options:
                return False
        elif actual != _normalize(expected):
            return False
    return True


class InMemoryVectorStoreProvider:
    def __init__(self, *, faults: FaultInjector | None = None) -> None:
        self._faults = faults or FaultInjector()
        # 作用域 → 语料 → id → 记录
        self._data: dict[str, dict[str, dict[str, VectorRecord]]] = {}

    def bind(self, tenant: TenantContext) -> InMemoryVectorStore:
        return InMemoryVectorStore(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> InMemoryVectorStore:
        return InMemoryVectorStore(self, platform_scope(purpose))

    async def aclose(self) -> None:
        self._data.clear()


class InMemoryVectorStore:
    def __init__(self, provider: InMemoryVectorStoreProvider, scope: Scope) -> None:
        self._provider = provider
        self._owner = scope_id(scope)

    def _corpus(self, corpus: str) -> dict[str, VectorRecord]:
        return self._provider._data.setdefault(self._owner, {}).setdefault(corpus, {})

    def _dims(self, records: dict[str, VectorRecord], version: str) -> int | None:
        return next((len(r.dense) for r in records.values() if r.embedding_version == version), None)

    async def upsert(self, corpus: str, records: Sequence[VectorRecord]) -> None:
        self._provider._faults.check("upsert")
        _check_corpus(corpus)
        stored = self._corpus(corpus)
        staged = dict(stored)
        for record in records:
            if not record.id:
                raise InvalidError("invalid_argument", "记录必须有 id")
            _check_fields(record.fields)
            staged.pop(record.id, None)
            dims = self._dims(staged, record.embedding_version)
            if dims is not None and dims != len(record.dense):
                raise InvalidError("dimension_mismatch", "同一语料、同一 embedding_version 下的向量维度必须一致")
            staged[record.id] = replace(
                record,
                dense=tuple(float(x) for x in record.dense),
                fields={k: _normalize(v) for k, v in record.fields.items()},
            )
        stored.clear()
        stored.update(staged)

    async def get(self, corpus: str, ids: Sequence[str]) -> list[VectorRecord]:
        self._provider._faults.check("get")
        _check_corpus(corpus)
        stored = self._corpus(corpus)
        return [stored[i] for i in ids if i in stored]

    async def delete(self, corpus: str, ids: Sequence[str]) -> int:
        self._provider._faults.check("delete")
        _check_corpus(corpus)
        stored = self._corpus(corpus)
        return sum(stored.pop(i, None) is not None for i in set(ids))

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
        self._provider._faults.check("query")
        _check_corpus(corpus)
        if top_k < 1:
            raise InvalidError("invalid_argument", "top_k 至少为 1")
        if dense is None and sparse is None:
            raise InvalidError("invalid_argument", "dense 与 sparse 至少给一个")
        if filter is not None:
            _check_fields(filter.must)
        stored = self._corpus(corpus)
        dims = self._dims(stored, embedding_version)
        if dense is not None and dims is not None and dims != len(dense):
            raise InvalidError("dimension_mismatch", "查询向量的维度与语料不一致")
        candidates = [r for r in stored.values() if r.embedding_version == embedding_version and _matches(dict(r.fields), filter)]

        dense_rank = (
            sorted(candidates, key=lambda r: (-_cosine(dense, r.dense), r.id)) if dense is not None else []
        )
        sparse_scored = (
            [(r, _dot(sparse, r.sparse)) for r in candidates if r.sparse is not None] if sparse is not None else []
        )
        sparse_rank = [r for r, s in sorted(sparse_scored, key=lambda x: (-x[1], x[0].id)) if s > 0]

        if dense is not None and sparse is not None:
            scores: dict[str, float] = {}
            for ranking in (dense_rank, sparse_rank):
                for rank, record in enumerate(ranking, start=1):
                    scores[record.id] = scores.get(record.id, 0.0) + 1.0 / (RRF_K + rank)
            by_id = {r.id: r for r in candidates}
            ordered = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
            hits = [Hit(id=i, score=s, fields=dict(by_id[i].fields)) for i, s in ordered]
        elif dense is not None:
            hits = [Hit(id=r.id, score=_cosine(dense, r.dense), fields=dict(r.fields)) for r in dense_rank]
        else:
            assert sparse is not None
            hits = [Hit(id=r.id, score=_dot(sparse, r.sparse), fields=dict(r.fields)) for r in sparse_rank]  # type: ignore[arg-type]
        return hits[:top_k]

    async def count(self, corpus: str, *, filter: Filter | None = None) -> int:
        self._provider._faults.check("count")
        _check_corpus(corpus)
        if filter is not None:
            _check_fields(filter.must)
        return sum(_matches(dict(r.fields), filter) for r in self._corpus(corpus).values())
