"""VectorStore 的一致性测试。语义见 agentruntime_core.providers.vector_store。"""

from __future__ import annotations

import pytest

from agentruntime_core.conformance.common import Harness, StandardRules, expect_error, tenant_a, tenant_b
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.providers.vector_store import (
    AnyOf,
    Filter,
    SparseVector,
    VectorRecord,
    VectorStore,
    VectorStoreProvider,
)

CORPUS = "conformance"
VERSION = "e3"

VectorHarness = Harness[VectorStoreProvider]


def record(id: str, dense: tuple[float, ...], *, sparse: SparseVector | None = None, version: str = VERSION, **fields) -> VectorRecord:
    return VectorRecord(id=id, dense=dense, embedding_version=version, sparse=sparse, fields=fields)


def sparse(**weights: float) -> SparseVector:
    """sparse(i0=1.0, i7=2.0) → indices (0, 7)。"""
    items = sorted((int(k[1:]), v) for k, v in weights.items())
    return SparseVector(indices=tuple(i for i, _ in items), values=tuple(v for _, v in items))


# 稠密排序 A > B > C > D；稀疏排序 B > C > D（A 与查询没有交集）。
# RRF（k = 60）：B = 1/62 + 1/61，C = 1/63 + 1/62，D = 1/64 + 1/63，A = 1/61（若稀疏结果含 A，再加 1/64），
# 融合后为 B > C > D > A，没有并列。
RANKING = [
    record("A", (1.0, 0.0), sparse=sparse(i1=5.0)),
    record("B", (0.9, 0.436), sparse=sparse(i0=3.0)),
    record("C", (0.6, 0.8), sparse=sparse(i0=2.0)),
    record("D", (0.2, 0.98), sparse=sparse(i0=1.0)),
]


class VectorStoreConformance(StandardRules):
    probe_operation = "upsert"

    async def write_probe(self, client: VectorStore, name: str) -> None:
        await client.upsert(CORPUS, [record(name, (1.0, 0.0))])

    async def probe_visible(self, client: VectorStore, name: str) -> bool:
        return bool(await client.get(CORPUS, [name]))

    @pytest.fixture
    def store(self, harness: VectorHarness) -> VectorStore:
        return harness.provider.bind(tenant_a())

    async def test_upsert_then_get(self, store: VectorStore) -> None:
        r = record("m1", (0.1, 0.2), sparse=sparse(i3=0.5), kind="failure", task_family=("code.fix_test",), trust=0.8)
        await store.upsert(CORPUS, [r])
        [got] = await store.get(CORPUS, ["m1", "missing"])
        assert got.id == "m1" and got.embedding_version == VERSION
        assert got.dense == pytest.approx((0.1, 0.2))
        assert got.sparse == sparse(i3=0.5)
        assert dict(got.fields) == {"kind": "failure", "task_family": ("code.fix_test",), "trust": 0.8}

    async def test_upsert_overwrites_by_id(self, store: VectorStore) -> None:
        await store.upsert(CORPUS, [record("m1", (1.0, 0.0), kind="fact")])
        await store.upsert(CORPUS, [record("m1", (1.0, 0.0), kind="fact")])
        await store.upsert(CORPUS, [record("m1", (0.0, 1.0), kind="success")])
        assert await store.count(CORPUS) == 1
        [got] = await store.get(CORPUS, ["m1"])
        assert got.fields["kind"] == "success"

    async def test_dense_query_orders_by_cosine(self, store: VectorStore) -> None:
        await store.upsert(CORPUS, RANKING)
        hits = await store.query(CORPUS, embedding_version=VERSION, top_k=4, dense=(1.0, 0.0))
        assert [h.id for h in hits] == ["A", "B", "C", "D"]
        assert hits[0].score >= hits[1].score >= hits[2].score >= hits[3].score

    async def test_sparse_query_orders_by_dot_product(self, store: VectorStore) -> None:
        await store.upsert(CORPUS, RANKING)
        hits = await store.query(CORPUS, embedding_version=VERSION, top_k=3, sparse=sparse(i0=1.0))
        assert [h.id for h in hits] == ["B", "C", "D"]

    async def test_hybrid_query_fuses_with_rrf(self, store: VectorStore) -> None:
        await store.upsert(CORPUS, RANKING)
        hits = await store.query(CORPUS, embedding_version=VERSION, top_k=4, dense=(1.0, 0.0), sparse=sparse(i0=1.0))
        assert [h.id for h in hits] == ["B", "C", "D", "A"]

    async def test_top_k_limits_results(self, store: VectorStore) -> None:
        await store.upsert(CORPUS, RANKING)
        hits = await store.query(CORPUS, embedding_version=VERSION, top_k=2, dense=(1.0, 0.0))
        assert [h.id for h in hits] == ["A", "B"]

    async def test_filters(self, store: VectorStore) -> None:
        await store.upsert(
            CORPUS,
            [
                record("p", (1.0, 0.0), kind="preference", tools=("github",)),
                record("f", (0.9, 0.1), kind="failure", tools=("docker", "github")),
                record("s", (0.8, 0.2), kind="success", tools=("docker",)),
            ],
        )

        async def ids(filter: Filter) -> list[str]:
            hits = await store.query(CORPUS, embedding_version=VERSION, top_k=10, dense=(1.0, 0.0), filter=filter)
            return [h.id for h in hits]

        assert await ids(Filter(must={"kind": "failure"})) == ["f"]
        assert await ids(Filter(must={"kind": AnyOf(("failure", "success"))})) == ["f", "s"]
        assert await ids(Filter(must={"tools": AnyOf(("github",))})) == ["p", "f"]
        assert await ids(Filter(must={"kind": "failure", "tools": AnyOf(("docker",))})) == ["f"]
        assert await store.count(CORPUS, filter=Filter(must={"tools": AnyOf(("docker",))})) == 2

    async def test_query_only_matches_same_embedding_version(self, store: VectorStore) -> None:
        await store.upsert(CORPUS, [record("m1", (1.0, 0.0), version="e3"), record("m1-new", (1.0, 0.0, 0.0), version="e4")])
        e3 = await store.query(CORPUS, embedding_version="e3", top_k=10, dense=(1.0, 0.0))
        e4 = await store.query(CORPUS, embedding_version="e4", top_k=10, dense=(1.0, 0.0, 0.0))
        assert [h.id for h in e3] == ["m1"]
        assert [h.id for h in e4] == ["m1-new"]

    async def test_dimension_mismatch_is_invalid(self, store: VectorStore) -> None:
        await store.upsert(CORPUS, [record("m1", (1.0, 0.0))])
        with expect_error(ErrorCategory.INVALID):
            await store.upsert(CORPUS, [record("m2", (1.0, 0.0, 0.0))])
        with expect_error(ErrorCategory.INVALID):
            await store.query(CORPUS, embedding_version=VERSION, top_k=1, dense=(1.0, 0.0, 0.0))

    @pytest.mark.parametrize("field", ["tenant_id", "scope"])
    async def test_reserved_fields_are_invalid(self, store: VectorStore, field: str) -> None:
        with expect_error(ErrorCategory.INVALID):
            await store.upsert(CORPUS, [record("m1", (1.0, 0.0), **{field: "t-beta"})])
        with expect_error(ErrorCategory.INVALID):
            await store.query(CORPUS, embedding_version=VERSION, top_k=1, dense=(1.0, 0.0), filter=Filter(must={field: "t-beta"}))

    async def test_query_needs_a_vector_and_positive_top_k(self, store: VectorStore) -> None:
        with expect_error(ErrorCategory.INVALID):
            await store.query(CORPUS, embedding_version=VERSION, top_k=5)
        with expect_error(ErrorCategory.INVALID):
            await store.query(CORPUS, embedding_version=VERSION, top_k=0, dense=(1.0, 0.0))

    @pytest.mark.parametrize("corpus", ["", "Memory", "1memory", "mem ory", "x" * 64])
    async def test_malformed_corpus_is_invalid(self, store: VectorStore, corpus: str) -> None:
        with expect_error(ErrorCategory.INVALID):
            await store.upsert(corpus, [record("m1", (1.0, 0.0))])

    async def test_query_on_empty_corpus_returns_nothing(self, store: VectorStore) -> None:
        assert await store.query("empty", embedding_version=VERSION, top_k=5, dense=(1.0, 0.0)) == []

    async def test_delete(self, store: VectorStore) -> None:
        await store.upsert(CORPUS, RANKING)
        assert await store.delete(CORPUS, ["A", "B", "missing"]) == 2
        assert await store.get(CORPUS, ["A", "B"]) == []
        hits = await store.query(CORPUS, embedding_version=VERSION, top_k=10, dense=(1.0, 0.0))
        assert [h.id for h in hits] == ["C", "D"]
        assert await store.delete(CORPUS, ["A"]) == 0

    async def test_other_tenant_cannot_query_or_delete(self, harness: VectorHarness, store: VectorStore) -> None:
        await store.upsert(CORPUS, RANKING)
        b = harness.provider.bind(tenant_b())
        assert await b.query(CORPUS, embedding_version=VERSION, top_k=10, dense=(1.0, 0.0)) == []
        assert await b.count(CORPUS) == 0
        assert await b.delete(CORPUS, ["A"]) == 0
        assert len(await store.get(CORPUS, ["A"])) == 1
