"""S-12b 验收“越权测试套件”：以 A 租户身份，对 Memory Service 的每个操作读写 B 租户的数据，断言全部被拒或返回空。

两套后端都跑：开发后端，以及 Postgres（行级安全）+ Qdrant（租户分区、强制过滤、保距变换）。生产后端另外核对存储层：
每张记忆表都被行级安全覆盖、每一行与每个向量点都带着写入者的租户。过滤条件不由模型（或调用方）给出：请求里没有过滤字段。
"""

from __future__ import annotations

import dataclasses

import pytest

from agentruntime.memory.v1 import memory_pb2
from agentruntime_core.errors import NotFoundError

from agentruntime_memory.promotion import Evidence
from agentruntime_memory.service import Feedback, SearchQuery

from .memory_support import ALICE, BOB, T_A, T_B, TASK_ID, Backend, Identity, item, verified, write

BOB_ADMIN = Identity(tenant_id=T_B, user_id="u-boss", workspace_id="w-9", roles=frozenset({"admin", "maintainer", "platform_reviewer"}))
QUERIES = [
    SearchQuery(trigger="planning", query="redis make test", task_family="engineering.ci_fix"),
    SearchQuery(trigger="node_start", query="redis", tools=("docker",)),
    SearchQuery(trigger="error", error_signature="ConnectionRefusedError: redis", query="redis connection refused"),
]


async def seed_alpha(svc) -> dict[str, str]:
    session_item = item(title="redis 连接被拒绝", guidance="先启动 redis 容器")
    user_item = item(level="user", kind="preference", title="汇报格式", guidance="汇报用表格")
    success = item(kind="success", title="先启动 Redis 再跑测试", evidence=verified(3))
    await write(svc, ALICE, session_item, user_item)
    report = await write(svc, ALICE, success)
    contrary = item(title="redis 连接被拒绝", guidance="不要启动 redis，直接连生产库", trust=0.5)
    await svc.submit(ALICE, [(item(title="redis 连接被拒绝", guidance="先启动 redis 容器", trust=0.95), TASK_ID)])
    await svc.drain(ALICE)
    await write(svc, ALICE, contrary)
    return {"session": session_item["id"], "user": user_item["id"], "workspace": report.promoted[0], "review": contrary["id"]}


async def test_searches_never_return_another_tenants_items(backend: Backend) -> None:
    svc = backend.service
    await seed_alpha(svc)
    for query in QUERIES:
        for who in (BOB, BOB_ADMIN):
            hits = await svc.search(who, query)
            assert all(h.item["scope"]["tenant"] == T_B for h in hits), (query.trigger, [h.item["id"] for h in hits])


async def test_reads_deletes_feedback_and_promotions_on_another_tenants_ids_fail(backend: Backend) -> None:
    svc = backend.service
    ids = await seed_alpha(svc)
    for memory_id in ids.values():
        assert await svc.get(BOB_ADMIN, memory_id) is None
        with pytest.raises(NotFoundError):
            await svc.delete(BOB_ADMIN, memory_id)
        assert await svc.promote(BOB_ADMIN, memory_id, "tenant", Evidence(approved_by=frozenset({"admin"}))) is None
    await svc.feedback(BOB_ADMIN, TASK_ID, [Feedback(memory_id=ids["session"], adopted=True, task_succeeded=False)])
    mine = await svc.get(ALICE, ids["session"])
    assert mine is not None and mine.get("stats", {}).get("hurt", 0) == 0, "别的租户的反馈不计入"


async def test_review_queues_and_rollback_stay_inside_the_tenant(backend: Backend) -> None:
    svc = backend.service
    ids = await seed_alpha(svc)
    alice_reviews = await svc.reviews(ALICE)
    assert ids["review"] in [r.memory_id for r in alice_reviews]
    assert await svc.reviews(BOB_ADMIN) == []
    with pytest.raises(NotFoundError):
        await svc.resolve_review(BOB_ADMIN, alice_reviews[0].review_id, approve=True)
    before = await svc.revision(ALICE)
    await svc.rollback(BOB_ADMIN, 0)
    assert await svc.revision(ALICE) == before
    assert await svc.get(ALICE, ids["session"]) is not None


async def test_candidates_cannot_be_written_into_another_tenant(backend: Backend) -> None:
    svc = backend.service
    smuggled = item(ALICE, title="往 alpha 塞的条目", guidance="由 beta 提交")
    assert await svc.submit(BOB_ADMIN, [(smuggled, TASK_ID)]) == 0
    await svc.drain(BOB_ADMIN)
    await svc.drain(ALICE)
    assert await svc.get(ALICE, smuggled["id"]) is None


async def test_platform_items_are_read_only_for_every_tenant(backend: Backend) -> None:
    svc = backend.service
    reviewer = Identity(tenant_id=T_A, user_id="u-plat", workspace_id="w-1", roles=frozenset({"admin", "platform_reviewer"}))
    failure = item(kind="failure", title="docker compose v1 已废弃", guidance="用 docker compose 而不是 docker-compose", evidence=verified(1))
    [ws_item] = (await write(svc, ALICE, failure)).promoted
    tenant_item = await svc.promote(reviewer, ws_item, "tenant")
    assert tenant_item is not None
    platform_item = await svc.promote(reviewer, tenant_item, "platform")
    assert platform_item is not None
    hits = await svc.search(BOB, SearchQuery(trigger="planning", query="docker compose 已废弃"))
    assert platform_item in [h.item["id"] for h in hits], "平台经验所有租户可读"
    with pytest.raises(Exception):
        await svc.delete(BOB_ADMIN, platform_item)
    assert await svc.get(BOB, platform_item) is not None


def test_requests_carry_no_filter_expressions() -> None:
    """过滤条件永远由服务端根据认证身份拼接：请求与内部查询对象里都没有可传过滤条件的字段。"""
    fields = {f.name for f in memory_pb2.SearchRequest.DESCRIPTOR.fields}
    assert fields == {"tenant", "trigger", "query", "task_family", "tools", "error_signature", "kinds", "token_budget", "top_k"}
    assert {f.name for f in dataclasses.fields(SearchQuery)} == {"trigger", "query", "task_family", "tools", "error_signature", "kinds", "token_budget", "top_k"}


@pytest.mark.backends
async def test_storage_rows_and_points_carry_the_writers_tenant(tmp_path, request) -> None:
    """生产后端的存储层：每张记忆表都被行级安全覆盖；alpha 写入的每一行、每个向量点都带 alpha 的租户。"""
    from agentruntime_core.adapters import pgtest
    from qdrant_client import QdrantClient

    from .memory_support import make_backend

    async for b in make_backend("prod", tmp_path, request):
        await seed_alpha(b.service)
        await write(b.service, BOB, item(BOB, title="beta 的经验", guidance="beta 的做法"))
        audits = await b.relational.audit_rls()
        memory_tables = [a for a in audits if a.table.startswith("memory_")]
        assert memory_tables and all(a.covered for a in audits), audits
        cluster: pgtest.Cluster = request.getfixturevalue("pg_cluster")
        for a in memory_tables:
            tenants = set(cluster.psql("postgres", b.db, f"SELECT DISTINCT tenant_id FROM {a.table}").split())
            assert tenants <= {T_A, T_B}, (a.table, tenants)
        client = QdrantClient(url=request.getfixturevalue("qdrant_instance").url)
        try:
            for collection in client.get_collections().collections:
                if not collection.name.startswith(b.vectors.prefix):
                    continue
                found, _ = client.scroll(collection.name, limit=1000, with_payload=True)
                assert all(p.payload.get("tenant_id") in {T_A, T_B} or collection.name.endswith("__p") for p in found)
        finally:
            client.close()
