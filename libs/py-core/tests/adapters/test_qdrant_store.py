"""S-12b：VectorStore 的 Qdrant 适配器（设计书 4.4、附录 I.6、I.7；计划 S-12b 交付“Qdrant 的租户字段分区与强制过滤”
“向量按租户密钥加密存储”）。

- 与开发实现同一套一致性测试（含租户绑定与隔离规则）。
- 存储层核对（直接用 qdrant-client 读原始数据，不经适配器）：每个点带租户字段，租户字段按分区建索引；
  入库的稠密向量与稀疏下标是变换过的，不同租户的同一个向量存得不一样；适配器取回时还原。
- 强制过滤：同一个集合里别的租户的点（哪怕向量一模一样）查不到、数不到、删不掉。
- 密钥来自密钥环，重启后不变；平台作用域用独立的集合。

用临时的 Qdrant 实例（qdranttest，二进制取自 PATH 或 AR_QDRANT_BIN）。每个用例用自己的集合前缀。
"""

from __future__ import annotations

import asyncio
import itertools
import math
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from qdrant_client import QdrantClient, models

from agentruntime_core.adapters import qdranttest
from agentruntime_core.adapters.keyring import RelationalKeyRing
from agentruntime_core.adapters.qdrant import TENANT_FIELD, QdrantVectorStoreProvider
from agentruntime_core.conformance.common import expect_error, tenant_a, tenant_b
from agentruntime_core.conformance.vector_store import CORPUS, VERSION, VectorStoreConformance, record, sparse
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.testing import FaultInjector

_prefixes = itertools.count(1)


def keyring(directory: Path) -> RelationalKeyRing:
    return RelationalKeyRing(SQLiteRelationalStoreProvider(directory / "keyring"), LocalFileSecretStoreProvider(directory / "secrets.json"))


class Harness:
    def __init__(self, url: str, directory: Path) -> None:
        self.faults = FaultInjector()
        self.prefix = f"t{next(_prefixes)}_{uuid.uuid4().hex[:6]}"
        self.provider = QdrantVectorStoreProvider(url=url, keys=keyring(directory), prefix=self.prefix, faults=self.faults)

    async def advance(self, delta: timedelta) -> None:
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)


class TestQdrantVectorStore(VectorStoreConformance):
    @pytest.fixture
    async def harness(self, qdrant_instance: qdranttest.Instance, tmp_path: Path):
        h = Harness(qdrant_instance.url, tmp_path)
        yield h
        await h.provider.aclose()


@pytest.fixture
async def harness(qdrant_instance: qdranttest.Instance, tmp_path: Path):
    h = Harness(qdrant_instance.url, tmp_path)
    yield h
    await h.provider.aclose()


@pytest.fixture
def raw(qdrant_instance: qdranttest.Instance):
    client = QdrantClient(url=qdrant_instance.url)
    yield client
    client.close()


def points(raw: QdrantClient, collection: str) -> list[models.Record]:
    found, _ = raw.scroll(collection, limit=1000, with_payload=True, with_vectors=True)
    return found


def dense_of(point: models.Record) -> list[float]:
    vector = point.vector
    if isinstance(vector, dict):
        vector = next(v for v in vector.values() if isinstance(v, list))
    assert isinstance(vector, list)
    return vector


def unit(v) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


async def test_every_point_carries_the_tenant_field_and_the_field_is_a_tenant_partition(harness: Harness, raw: QdrantClient) -> None:
    await harness.provider.bind(tenant_a()).upsert(CORPUS, [record("m1", (0.6, 0.8))])
    await harness.provider.bind(tenant_b()).upsert(CORPUS, [record("m1", (0.6, 0.8))])
    collection = harness.provider.collection_name(CORPUS, VERSION)
    stored = points(raw, collection)
    assert len(stored) == 2, "同一个记录 id 在两个租户下是两个点，互不覆盖"
    assert {p.payload[TENANT_FIELD] for p in stored} == {tenant_a().tenant_id, tenant_b().tenant_id}
    schema = raw.get_collection(collection).payload_schema[TENANT_FIELD]
    assert schema.data_type == models.PayloadSchemaType.KEYWORD
    assert getattr(schema.params, "is_tenant", False) is True


async def test_stored_vectors_are_transformed_with_the_tenants_key(harness: Harness, raw: QdrantClient) -> None:
    plain = (0.6, 0.8, 0.0, 0.0)
    await harness.provider.bind(tenant_a()).upsert(CORPUS, [record("m1", plain, sparse=sparse(i3=0.5, i9=1.0))])
    await harness.provider.bind(tenant_b()).upsert(CORPUS, [record("m1", plain, sparse=sparse(i3=0.5, i9=1.0))])
    stored = {p.payload[TENANT_FIELD]: p for p in points(raw, harness.provider.collection_name(CORPUS, VERSION))}
    a, b = dense_of(stored[tenant_a().tenant_id]), dense_of(stored[tenant_b().tenant_id])
    assert a != pytest.approx(unit(plain), abs=1e-3), "入库的向量不是原向量"
    assert a != pytest.approx(b, abs=1e-3), "不同租户的同一个向量存得不一样"
    raw_sparse = stored[tenant_a().tenant_id].vector
    sparse_vectors = [v for v in raw_sparse.values() if isinstance(v, models.SparseVector)] if isinstance(raw_sparse, dict) else []
    assert sparse_vectors and set(sparse_vectors[0].indices) != {3, 9}, "稀疏下标也是变换过的"
    [got] = await harness.provider.bind(tenant_a()).get(CORPUS, ["m1"])
    assert got.dense == pytest.approx(plain, abs=1e-6), "适配器取回时还原"
    assert got.sparse == sparse(i3=0.5, i9=1.0)


async def test_points_of_other_tenants_in_the_same_collection_are_filtered_out(harness: Harness, raw: QdrantClient) -> None:
    """强制过滤：哪怕别的租户的点与查询向量一模一样，也查不到、数不到、删不掉。"""
    a = harness.provider.bind(tenant_a())
    await a.upsert(CORPUS, [record("mine", (1.0, 0.0), kind="fact")])
    collection = harness.provider.collection_name(CORPUS, VERSION)
    [mine] = points(raw, collection)
    intruder = models.PointStruct(id=str(uuid.uuid4()), vector=mine.vector, payload={**mine.payload, TENANT_FIELD: "t-intruder", "_id": "theirs"})
    raw.upsert(collection, [intruder])
    hits = await a.query(CORPUS, embedding_version=VERSION, top_k=10, dense=(1.0, 0.0))
    assert [h.id for h in hits] == ["mine"]
    assert await a.count(CORPUS) == 1
    assert await a.delete(CORPUS, ["theirs"]) == 0
    assert len(points(raw, collection)) == 2


async def test_keys_survive_a_provider_restart(qdrant_instance: qdranttest.Instance, tmp_path: Path) -> None:
    prefix = f"restart_{uuid.uuid4().hex[:6]}"
    first = QdrantVectorStoreProvider(url=qdrant_instance.url, keys=keyring(tmp_path), prefix=prefix)
    await first.bind(tenant_a()).upsert(CORPUS, [record("m1", (0.3, 0.4))])
    await first.aclose()
    second = QdrantVectorStoreProvider(url=qdrant_instance.url, keys=keyring(tmp_path), prefix=prefix)
    try:
        [got] = await second.bind(tenant_a()).get(CORPUS, ["m1"])
        assert got.dense == pytest.approx((0.3, 0.4), abs=1e-6)
        hits = await second.bind(tenant_a()).query(CORPUS, embedding_version=VERSION, top_k=1, dense=(0.3, 0.4))
        assert hits[0].score == pytest.approx(1.0, abs=1e-6)
    finally:
        await second.aclose()


async def test_platform_scope_uses_separate_collections(harness: Harness, raw: QdrantClient) -> None:
    await harness.provider.bind_platform("memory").upsert(CORPUS, [record("p1", (1.0, 0.0))])
    platform_collection = harness.provider.collection_name(CORPUS, VERSION, platform=True)
    assert platform_collection != harness.provider.collection_name(CORPUS, VERSION)
    assert len(points(raw, platform_collection)) == 1
    assert await harness.provider.bind(tenant_a()).query(CORPUS, embedding_version=VERSION, top_k=5, dense=(1.0, 0.0)) == []


@pytest.mark.parametrize("field", ["_id", "_norm", "_anything"])
async def test_underscore_fields_are_reserved_for_the_adapter(harness: Harness, field: str) -> None:
    with expect_error(ErrorCategory.INVALID):
        await harness.provider.bind(tenant_a()).upsert(CORPUS, [record("m1", (1.0, 0.0), **{field: "x"})])
