"""T-18：VectorStore 的 Milvus 适配器（设计改动草稿 C1：默认改用 Milvus；设计书 4.4、附录 I.6、I.7 的隔离与加密要求不变）。

- 与开发实现、Qdrant 适配器同一套一致性测试（含租户绑定与隔离规则）。
- 存储层核对（直接用 pymilvus 读原始数据，不经适配器）：租户字段是集合的 partition key，每行都带它；入库的稠密向量与
  稀疏下标是按租户密钥变换过的，不同租户的同一个向量存得不一样；适配器取回时还原。
- 强制过滤：同一个集合里别的租户的行（哪怕向量一模一样）查不到、数不到、删不掉。
- 过滤表达式注入：过滤值里的引号与运算符不能越过租户条件；字段名不是普通标识符时 invalid。
- 写后即读：upsert 之后马上查得到（Milvus 默认的 Bounded 一致性做不到，适配器要用 Strong）。
- 后端不可用报 transient，不返回空结果冒充“没有命中”。
- 密钥来自密钥环，重启后不变；平台作用域用独立的集合。

用临时的 Milvus 单机版容器（milvustest，docker 取自 PATH 或 AR_DOCKER_BIN）。每个用例用自己的集合前缀。
"""

from __future__ import annotations

import asyncio
import itertools
import math
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pymilvus import MilvusClient

from agentruntime_core.adapters import milvustest
from agentruntime_core.adapters.keyring import RelationalKeyRing
from agentruntime_core.adapters.milvus import DENSE, PRIMARY_FIELD, RECORD_ID_FIELD, SPARSE, TENANT_FIELD, MilvusVectorStoreProvider
from agentruntime_core.conformance.common import expect_error, tenant_a, tenant_b
from agentruntime_core.conformance.vector_store import CORPUS, VERSION, VectorStoreConformance, record, sparse
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.providers.vector_store import Filter
from agentruntime_core.testing import FaultInjector

_prefixes = itertools.count(1)


def keyring(directory: Path) -> RelationalKeyRing:
    return RelationalKeyRing(SQLiteRelationalStoreProvider(directory / "keyring"), LocalFileSecretStoreProvider(directory / "secrets.json"))


class Harness:
    def __init__(self, uri: str, directory: Path) -> None:
        self.faults = FaultInjector()
        self.prefix = f"t{next(_prefixes)}_{uuid.uuid4().hex[:6]}"
        self.provider = MilvusVectorStoreProvider(uri=uri, keys=keyring(directory), prefix=self.prefix, faults=self.faults)

    async def advance(self, delta: timedelta) -> None:
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)


class TestMilvusVectorStore(VectorStoreConformance):
    @pytest.fixture
    async def harness(self, milvus_instance: milvustest.Instance, tmp_path: Path):
        h = Harness(milvus_instance.uri, tmp_path)
        yield h
        await h.provider.aclose()


@pytest.fixture
async def harness(milvus_instance: milvustest.Instance, tmp_path: Path):
    h = Harness(milvus_instance.uri, tmp_path)
    yield h
    await h.provider.aclose()


@pytest.fixture
def raw(milvus_instance: milvustest.Instance):
    client = MilvusClient(uri=milvus_instance.uri)
    yield client
    client.close()


def rows(raw: MilvusClient, collection: str) -> list[dict[str, Any]]:
    """集合里的全部行（含动态字段），用 Strong 一致性读，不受写入延迟影响。"""
    return raw.query(collection, filter=f'{TENANT_FIELD} != ""', output_fields=["*"], limit=1000, consistency_level="Strong")


def unit(v) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def sparse_indices(value: Any) -> set[int]:
    """pymilvus 读回的稀疏向量是 {下标: 权重}（下标可能是字符串）。"""
    return {int(k) for k in dict(value)}


async def test_tenant_field_is_the_partition_key_and_every_row_carries_it(harness: Harness, raw: MilvusClient) -> None:
    await harness.provider.bind(tenant_a()).upsert(CORPUS, [record("m1", (0.6, 0.8))])
    await harness.provider.bind(tenant_b()).upsert(CORPUS, [record("m1", (0.6, 0.8))])
    collection = harness.provider.collection_name(CORPUS, VERSION)
    stored = rows(raw, collection)
    assert len(stored) == 2, "同一个记录 id 在两个租户下是两行，互不覆盖"
    assert {r[TENANT_FIELD] for r in stored} == {tenant_a().tenant_id, tenant_b().tenant_id}
    assert {r[RECORD_ID_FIELD] for r in stored} == {"m1"}
    assert len({r[PRIMARY_FIELD] for r in stored}) == 2, "主键由（作用域、语料、记录 id）派生，两个租户不同"
    fields = {f["name"]: f for f in raw.describe_collection(collection)["fields"]}
    assert fields[TENANT_FIELD].get("is_partition_key") is True, "租户字段是 partition key"


async def test_stored_vectors_are_transformed_with_the_tenants_key(harness: Harness, raw: MilvusClient) -> None:
    plain = (0.6, 0.8, 0.0, 0.0)
    await harness.provider.bind(tenant_a()).upsert(CORPUS, [record("m1", plain, sparse=sparse(i3=0.5, i9=1.0))])
    await harness.provider.bind(tenant_b()).upsert(CORPUS, [record("m1", plain, sparse=sparse(i3=0.5, i9=1.0))])
    stored = {r[TENANT_FIELD]: r for r in rows(raw, harness.provider.collection_name(CORPUS, VERSION))}
    a, b = list(stored[tenant_a().tenant_id][DENSE]), list(stored[tenant_b().tenant_id][DENSE])
    assert a != pytest.approx(unit(plain), abs=1e-3), "入库的向量不是原向量"
    assert a != pytest.approx(b, abs=1e-3), "不同租户的同一个向量存得不一样"
    assert sparse_indices(stored[tenant_a().tenant_id][SPARSE]) != {3, 9}, "稀疏下标也是变换过的"
    [got] = await harness.provider.bind(tenant_a()).get(CORPUS, ["m1"])
    assert got.dense == pytest.approx(plain, abs=1e-6), "适配器取回时还原"
    assert got.sparse == sparse(i3=0.5, i9=1.0)


async def _plant_intruder(harness: Harness, raw: MilvusClient, **changes: Any) -> str:
    """把租户 A 的唯一一行复制一份，改成别的租户的（向量一模一样），直接写进同一个集合。"""
    collection = harness.provider.collection_name(CORPUS, VERSION)
    [mine] = rows(raw, collection)
    intruder = {**mine, TENANT_FIELD: "t-intruder", PRIMARY_FIELD: uuid.uuid4().hex, RECORD_ID_FIELD: "theirs", **changes}
    raw.insert(collection, [intruder])
    return collection


async def test_rows_of_other_tenants_in_the_same_collection_are_filtered_out(harness: Harness, raw: MilvusClient) -> None:
    """强制过滤：哪怕别的租户的行与查询向量一模一样，也查不到、数不到、删不掉。"""
    a = harness.provider.bind(tenant_a())
    await a.upsert(CORPUS, [record("mine", (1.0, 0.0), sparse=sparse(i1=1.0), kind="fact")])
    collection = await _plant_intruder(harness, raw)
    hits = await a.query(CORPUS, embedding_version=VERSION, top_k=10, dense=(1.0, 0.0), sparse=sparse(i1=1.0))
    assert [h.id for h in hits] == ["mine"]
    assert await a.count(CORPUS) == 1
    assert await a.get(CORPUS, ["theirs"]) == []
    assert await a.delete(CORPUS, ["theirs"]) == 0
    assert len(rows(raw, collection)) == 2


@pytest.mark.parametrize(
    "value",
    [
        'fact" || tenant_id != "',
        "fact' || tenant_id != '",
        'x" or tenant_id == "t-intruder',
        "fact\\",
    ],
)
async def test_filter_values_cannot_escape_the_tenant_condition(harness: Harness, raw: MilvusClient, value: str) -> None:
    """过滤值经表达式模板参数传入，不拼接字符串：值里的引号与运算符不会变成表达式的一部分。"""
    a = harness.provider.bind(tenant_a())
    await a.upsert(CORPUS, [record("mine", (1.0, 0.0), kind="fact")])
    await _plant_intruder(harness, raw)
    hits = await a.query(CORPUS, embedding_version=VERSION, top_k=10, dense=(1.0, 0.0), filter=Filter(must={"kind": value}))
    assert hits == [], "没有哪一行的 kind 等于这个值；别的租户的行更不能出现"
    assert await a.count(CORPUS, filter=Filter(must={"kind": value})) == 0


@pytest.mark.parametrize("field", ['kind" or 1 == 1 or "', "kind == 'x'", "a b", "$meta", "1kind", "kind[0]"])
async def test_field_names_that_are_not_plain_identifiers_are_invalid(harness: Harness, field: str) -> None:
    """字段名进不了模板参数，只接受普通标识符（字母开头，字母、数字、下划线）。"""
    a = harness.provider.bind(tenant_a())
    with expect_error(ErrorCategory.INVALID):
        await a.query(CORPUS, embedding_version=VERSION, top_k=1, dense=(1.0, 0.0), filter=Filter(must={field: "x"}))
    with expect_error(ErrorCategory.INVALID):
        await a.upsert(CORPUS, [record("m1", (1.0, 0.0), **{field: "x"})])


async def test_writes_are_visible_to_the_next_read_immediately(harness: Harness) -> None:
    """写后即读：每次 upsert 之后马上 count、get、query 都看得到（适配器用 Strong 一致性）。"""
    a = harness.provider.bind(tenant_a())
    for i in range(1, 6):
        await a.upsert(CORPUS, [record(f"m{i}", (1.0, float(i)))])
        assert await a.count(CORPUS) == i
        assert [r.id for r in await a.get(CORPUS, [f"m{i}"])] == [f"m{i}"]
        hits = await a.query(CORPUS, embedding_version=VERSION, top_k=10, dense=(1.0, float(i)))
        assert hits and hits[0].id == f"m{i}"
    assert await a.delete(CORPUS, ["m5"]) == 1
    assert await a.count(CORPUS) == 4


async def test_an_unreachable_milvus_is_transient_not_empty(tmp_path: Path) -> None:
    """后端不可用报 transient，不返回空结果冒充“没有命中”；建客户端时不连接。"""
    provider = MilvusVectorStoreProvider(uri="http://127.0.0.1:1", keys=keyring(tmp_path), prefix="down", timeout_s=2)
    try:
        store = provider.bind(tenant_a())
        with expect_error(ErrorCategory.TRANSIENT):
            await store.query(CORPUS, embedding_version=VERSION, top_k=1, dense=(1.0, 0.0))
        with expect_error(ErrorCategory.TRANSIENT):
            await store.upsert(CORPUS, [record("m1", (1.0, 0.0))])
    finally:
        await provider.aclose()


async def test_keys_survive_a_provider_restart(milvus_instance: milvustest.Instance, tmp_path: Path) -> None:
    prefix = f"restart_{uuid.uuid4().hex[:6]}"
    first = MilvusVectorStoreProvider(uri=milvus_instance.uri, keys=keyring(tmp_path), prefix=prefix)
    await first.bind(tenant_a()).upsert(CORPUS, [record("m1", (0.3, 0.4))])
    await first.aclose()
    second = MilvusVectorStoreProvider(uri=milvus_instance.uri, keys=keyring(tmp_path), prefix=prefix)
    try:
        [got] = await second.bind(tenant_a()).get(CORPUS, ["m1"])
        assert got.dense == pytest.approx((0.3, 0.4), abs=1e-6)
        hits = await second.bind(tenant_a()).query(CORPUS, embedding_version=VERSION, top_k=1, dense=(0.3, 0.4))
        assert hits[0].score == pytest.approx(1.0, abs=1e-5)
    finally:
        await second.aclose()


async def test_platform_scope_uses_separate_collections(harness: Harness, raw: MilvusClient) -> None:
    await harness.provider.bind_platform("memory").upsert(CORPUS, [record("p1", (1.0, 0.0))])
    platform_collection = harness.provider.collection_name(CORPUS, VERSION, platform=True)
    assert platform_collection != harness.provider.collection_name(CORPUS, VERSION)
    assert len(rows(raw, platform_collection)) == 1
    assert await harness.provider.bind(tenant_a()).query(CORPUS, embedding_version=VERSION, top_k=5, dense=(1.0, 0.0)) == []


@pytest.mark.parametrize("field", ["_id", "_norm", "_anything"])
async def test_underscore_fields_are_reserved_for_the_adapter(harness: Harness, field: str) -> None:
    with expect_error(ErrorCategory.INVALID):
        await harness.provider.bind(tenant_a()).upsert(CORPUS, [record("m1", (1.0, 0.0), **{field: "x"})])
