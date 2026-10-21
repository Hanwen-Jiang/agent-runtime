"""S-13 验收“回滚测试”：记忆库版本化，可回滚（附录 I.6、I.7）。两套后端都跑。

回滚把修订号之后的变更按相反顺序撤销并重建索引；用户删除的条目不会被回滚恢复（删除是遗忘请求，不是可撤销的编辑）。
"""

from __future__ import annotations

from agentruntime_memory.service import CORPUS, SearchQuery

from .memory_support import ADMIN, ALICE, Backend, item, verified, write


async def ids(svc, identity, query: str) -> list[str]:
    return [h.item["id"] for h in await svc.search(identity, SearchQuery(trigger="planning", query=query, top_k=20))]


async def test_rollback_undoes_later_writes_merges_and_promotions(backend: Backend) -> None:
    svc = backend.service
    good = item(kind="success", title="先启动 Redis 再跑测试", evidence=verified(1))
    await write(svc, ALICE, good)
    checkpoint = await svc.revision(ADMIN)
    # 之后：合并出版本 2（并因此晋升到 Workspace），以及一条新条目。
    report = await write(svc, ALICE, item(kind="success", title="先启动 Redis 再跑测试", evidence=verified(3)[1:]))
    assert report.merged == [good["id"]] and report.promoted
    later = item(kind="failure", title="docker 磁盘满", guidance="清理镜像")
    await write(svc, ALICE, later)

    result = await svc.rollback(ADMIN, checkpoint)
    assert result.to_revision == checkpoint and result.from_revision > checkpoint
    restored = await svc.get(ALICE, good["id"])
    assert restored is not None and restored["version"] == 1 and len(restored["evidence"]) == 1
    assert await svc.get(ALICE, later["id"]) is None
    assert await svc.get(ALICE, report.promoted[0]) is None
    assert await ids(svc, ALICE, "docker 磁盘满") == []
    assert await backend.vectors.bind(ALICE.tenant_context()).get(CORPUS, [later["id"]]) == [], "索引随之重建"
    assert await svc.revision(ADMIN) > result.from_revision, "回滚本身也记一个修订"


async def test_rollback_never_resurrects_deleted_items(backend: Backend) -> None:
    svc = backend.service
    entry = item(title="个人邮箱签名", guidance="签名用中文全名")
    await write(svc, ALICE, entry)
    checkpoint = await svc.revision(ADMIN)
    await svc.delete(ALICE, entry["id"], reason="用户要求遗忘")
    result = await svc.rollback(ADMIN, checkpoint)
    assert entry["id"] in result.kept_deleted
    assert await svc.get(ALICE, entry["id"]) is None


async def test_rollback_only_touches_the_admins_tenant(backend: Backend) -> None:
    from .memory_support import BOB

    svc = backend.service
    await write(svc, BOB, item(BOB, title="beta 的经验", guidance="beta 租户的做法"))
    bob_rev = await svc.revision(BOB)
    await svc.rollback(ADMIN, 0)
    assert await svc.revision(BOB) == bob_rev
    assert await ids(svc, BOB, "beta 的经验")
