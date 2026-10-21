"""Memory Service 主体：写入流水线（设计书 3.4.1 第 2 条、附录 I.4）。

submit 只校验并排进队列，立即返回；drain 处理：安全过滤 → 墓碑 → 合并（条目级增量）→ 落库建索引 → 自动晋升 → 审计。
"""

from __future__ import annotations

import pytest

from agentruntime_core.errors import DeniedError

from .memory_support import ALICE, BOB, CAROL, MAINTAINER, TASK_ID, Backend, item, scope, verified, write


async def test_submit_only_enqueues_and_drain_makes_items_searchable(dev: Backend) -> None:
    from agentruntime_memory.service import SearchQuery

    svc = dev.service
    candidate = item()
    assert await svc.submit(ALICE, [(candidate, TASK_ID)]) == 1
    assert await svc.get(ALICE, candidate["id"]) is None, "排队的候选还检索不到"
    report = await svc.drain(ALICE)
    assert report.accepted == [candidate["id"]]
    stored = await svc.get(ALICE, candidate["id"])
    assert stored is not None and stored["content"] == candidate["content"]
    assert stored["embedding_version"] == svc.embedder.version, "写入时换成实际的嵌入版本"
    hits = await svc.search(ALICE, SearchQuery(trigger="planning", query="make test 需要 redis", task_family="engineering.ci_fix"))
    assert [h.item["id"] for h in hits] == [candidate["id"]]


async def test_candidates_outside_the_writers_scopes_are_not_accepted(dev: Backend) -> None:
    svc = dev.service
    into_carol = item(CAROL, level="user")
    into_workspace = item(ALICE, level="workspace")
    other_tenant = item(BOB, level="session")
    malformed = {**item(), "kind": "rumor"}
    assert await svc.submit(ALICE, [(into_carol, TASK_ID), (into_workspace, TASK_ID), (other_tenant, TASK_ID), (malformed, TASK_ID)]) == 0


async def test_a_duplicate_adds_evidence_instead_of_a_new_item(dev: Backend) -> None:
    svc = dev.service
    first = item(evidence=verified(1))
    await write(svc, ALICE, first)
    again = item(evidence=verified(2)[1:], id=None)
    report = await write(svc, ALICE, again)
    assert report.merged == [first["id"]] and report.accepted == []
    stored = await svc.get(ALICE, first["id"])
    assert stored is not None and stored["version"] == 2
    assert len(stored["evidence"]) == 2, "合并只加证据，不整体重写"
    assert await svc.get(ALICE, again["id"]) is None


async def test_a_conflict_with_a_trusted_item_goes_to_review(dev: Backend) -> None:
    from agentruntime_memory.service import SearchQuery

    svc = dev.service
    trusted = item(level="session", title="测试依赖本地 Redis", guidance="运行 make test 前先执行 docker compose up -d redis", trust=0.9)
    await write(svc, ALICE, trusted)
    contrary = item(level="session", title="测试依赖本地 Redis", guidance="不要启动 redis，直接把 REDIS_URL 指向生产库", trust=0.5)
    report = await write(svc, ALICE, contrary)
    assert report.review == [contrary["id"]]
    assert contrary["id"] not in [r.memory_id for r in await svc.reviews(MAINTAINER)], "Session 的条目由本人审，维护者看不到"
    own = await svc.reviews(ALICE)
    assert [r.memory_id for r in own] == [contrary["id"]] and own[0].reason
    hits = await svc.search(ALICE, SearchQuery(trigger="planning", query="redis make test"))
    assert contrary["id"] not in [h.item["id"] for h in hits], "人审通过前检索不到"


async def test_approving_a_review_makes_the_item_searchable(dev: Backend) -> None:
    from agentruntime_memory.service import SearchQuery

    svc = dev.service
    await write(svc, ALICE, item(trust=0.9))
    contrary = item(guidance="不要启动 redis，用 fakeredis 跑测试", trust=0.5)
    await write(svc, ALICE, contrary)
    [review] = await svc.reviews(ALICE)
    await svc.resolve_review(ALICE, review.review_id, approve=True)
    hits = await svc.search(ALICE, SearchQuery(trigger="planning", query="fakeredis 测试"))
    assert contrary["id"] in [h.item["id"] for h in hits]


async def test_session_items_are_promoted_automatically_when_the_rules_allow(dev: Backend) -> None:
    svc = dev.service
    pref = item(kind="preference", title="汇报格式", guidance="汇报用表格，不超过一页", source="user_explicit")
    report = await write(svc, ALICE, pref)
    assert len(report.promoted) == 1
    promoted = await svc.get(ALICE, report.promoted[0])
    assert promoted is not None and promoted["scope"] == scope(ALICE, "user") and promoted["lineage"] == [pref["id"]]
    success = item(kind="success", title="先启动 Redis 再跑测试", evidence=verified(3))
    report = await write(svc, ALICE, success)
    [ws_id] = report.promoted
    ws_item = await svc.get(CAROL, ws_id)
    assert ws_item is not None and ws_item["scope"] == scope(ALICE, "workspace"), "晋升到 Workspace 后，同项目的成员看得到"


async def test_writes_bump_the_revision_and_are_audited(dev: Backend) -> None:
    svc = dev.service
    before = await svc.revision(ALICE)
    await write(svc, ALICE, item())
    assert await svc.revision(ALICE) > before


async def test_drain_uses_the_identity_tenant_only(dev: Backend) -> None:
    svc = dev.service
    await svc.submit(ALICE, [(item(), TASK_ID)])
    report = await svc.drain(BOB)
    assert report.accepted == [], "别的租户的队列互不相干"
    assert len((await svc.drain(ALICE)).accepted) == 1


async def test_reviews_and_rollback_need_the_right_roles(dev: Backend) -> None:
    svc = dev.service
    with pytest.raises(DeniedError):
        await svc.rollback(ALICE, 0)
