"""S-13 验收“级联删除测试”：删除源条目后，派生条目与向量全部消失；墓碑防止同一内容被重新写入（附录 I.6、I.7）。两套后端都跑。"""

from __future__ import annotations

import pytest

from agentruntime_core.errors import DeniedError, NotFoundError
from agentruntime_core.providers.vector_store import VectorStore

from agentruntime_memory.service import CORPUS

from .memory_support import ADMIN, ALICE, BOB, CAROL, MAINTAINER, Backend, item, verified, write


def vectors_of(backend: Backend, identity) -> VectorStore:
    return backend.vectors.bind(identity.tenant_context())


async def test_deleting_a_source_removes_its_derived_items_and_vectors(backend: Backend) -> None:
    svc = backend.service
    source = item(kind="success", title="先启动 Redis 再跑测试", evidence=verified(3))
    report = await write(svc, ALICE, source)
    [derived] = report.promoted
    assert await svc.get(CAROL, derived) is not None
    deleted = await svc.delete(ALICE, source["id"], reason="用户要求删除")
    assert set(deleted) == {source["id"], derived}
    assert await svc.get(ALICE, source["id"]) is None and await svc.get(CAROL, derived) is None
    assert await vectors_of(backend, ALICE).get(CORPUS, [source["id"], derived]) == [], "向量（稠密与稀疏）一并删除"


async def test_deletion_cascades_transitively(backend: Backend) -> None:
    svc = backend.service
    failure = item(kind="failure", title="分页参数是 cursor", guidance="该 API 分页参数是 cursor 不是 page", evidence=verified(1))
    report = await write(svc, ALICE, failure)
    [ws_item] = report.promoted
    tenant_item = await svc.promote(ADMIN, ws_item, "tenant")
    assert tenant_item is not None
    deleted = await svc.delete(ALICE, failure["id"])
    assert set(deleted) == {failure["id"], ws_item, tenant_item}


async def test_a_tombstone_blocks_the_same_content_from_coming_back(backend: Backend) -> None:
    svc = backend.service
    entry = item(title="内部报销口径", guidance="差旅报销按实际票据金额")
    await write(svc, ALICE, entry)
    await svc.delete(ALICE, entry["id"])
    again = item(title="内部报销口径", guidance="差旅报销按实际票据金额")
    report = await write(svc, ALICE, again)
    assert report.accepted == [] and len(report.rejected) == 1
    assert await svc.get(ALICE, again["id"]) is None


async def test_deletion_rights_and_visibility(backend: Backend) -> None:
    svc = backend.service
    entry = item(level="user", title="汇报格式", guidance="汇报用表格")
    await write(svc, ALICE, entry)
    with pytest.raises(NotFoundError):
        await svc.delete(CAROL, entry["id"])
    with pytest.raises(NotFoundError):
        await svc.delete(BOB, entry["id"])
    success = item(kind="success", title="先启动 Redis 再跑测试", evidence=verified(3))
    [ws_item] = (await write(svc, ALICE, success)).promoted
    with pytest.raises(DeniedError):
        await svc.delete(CAROL, ws_item)
    assert await svc.delete(MAINTAINER, ws_item) == [ws_item]
