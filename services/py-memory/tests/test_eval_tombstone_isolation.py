"""Real MemoryService proof for per-question evaluation cleanup.

Production deletion must keep preventing resurrection. Only the explicitly
identified isolated test tenant may remove its own recorded test tombstones;
neither another workspace nor a new random memory ID supplies that isolation.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from agentruntime_memory.identity import Identity
from agentruntime_memory.service import CORPUS, SearchQuery

from .memory_support import TASK_ID, Backend, item, verified, write


FIRST = Identity(tenant_id="evalmem-proof", user_id="u-eval", workspace_id="case-one")
SECOND = replace(FIRST, workspace_id="case-two")
OTHER = replace(FIRST, tenant_id="other-proof")


def fact(identity: Identity, title="Harbor release uses cobalt", guidance="Harbor release uses cobalt transport.", **kwargs):
    return item(identity, level="user_workspace", kind="fact", title=title, guidance=guidance,
                tools=(), task_family=(), evidence=verified(2), **kwargs)


async def stored_ids(backend: Backend, identity: Identity, table="memory_tombstones"):
    assert table in ("memory_tombstones", "memory_items", "memory_lineage")
    column = {"memory_tombstones": "memory_id", "memory_items": "id", "memory_lineage": "child"}[table]
    async with backend.relational.bind(identity.tenant_context()).transaction(read_only=True) as tx:
        return {str(r[column]) for r in await tx.query(f"SELECT {column} FROM {table}")}


async def clear_recorded_test_tombstones(backend: Backend, identity: Identity, recorded_ids: list[str]):
    """Exercise tenant-bound, exact-ID SQL without changing service deletion."""
    if identity.tenant_id != FIRST.tenant_id or not recorded_ids:
        raise ValueError("only the explicitly isolated evaluation tenant")
    async with backend.relational.bind(identity.tenant_context()).transaction() as tx:
        row = await tx.query_one("SELECT COUNT(*) AS n FROM memory_queue")
        if row["n"]:
            raise ValueError("drain evaluation writes before cleanup")
        for memory_id in recorded_ids:
            if await tx.query_one("SELECT id FROM memory_items WHERE id = $1", (memory_id,)):
                raise ValueError("delete recorded live items before clearing their tombstones")
        for memory_id in recorded_ids:
            await tx.execute("DELETE FROM memory_tombstones WHERE memory_id = $1", (memory_id,))


async def test_workspace_change_does_not_hide_tombstones_and_exact_cleanup_restores_next_question(dev: Backend):
    svc = dev.service
    first = fact(FIRST)
    assert (await write(svc, FIRST, first)).accepted == [first["id"]]
    before = await svc.search(FIRST, SearchQuery(trigger="planning", query=first["content"]["guidance"], top_k=50))
    assert [h.item["id"] for h in before] == [first["id"]]
    deleted = await svc.delete(FIRST, first["id"], "isolated evaluation cleanup")
    assert deleted == [first["id"]]
    assert await dev.vectors.bind(FIRST.tenant_context()).get(CORPUS, deleted) == []

    second = fact(SECOND)
    rejected = await write(svc, SECOND, second)
    assert rejected.accepted == [] and rejected.rejected == [f"{second['id']}：tombstoned"]
    assert await svc.search(SECOND, SearchQuery(trigger="planning", query=second["content"]["guidance"], top_k=50)) == []

    unrelated = fact(FIRST, title="Orchard arithmetic reference", guidance="Orchard count is seventy-nine.")
    await write(svc, FIRST, unrelated)
    await svc.delete(FIRST, unrelated["id"])
    # The same literal ID in another tenant tests the tenant binding directly.
    foreign = fact(OTHER, id=first["id"])
    await write(svc, OTHER, foreign)
    await svc.delete(OTHER, foreign["id"])
    assert await stored_ids(dev, FIRST) == {first["id"], unrelated["id"]}
    assert await stored_ids(dev, OTHER) == {foreign["id"]}

    await clear_recorded_test_tombstones(dev, FIRST, deleted)
    assert await stored_ids(dev, FIRST) == {unrelated["id"]}
    assert await stored_ids(dev, OTHER) == {foreign["id"]}, "same-ID tombstone in another tenant must survive"
    repeated = fact(SECOND)
    assert (await write(svc, SECOND, repeated)).accepted == [repeated["id"]]
    found = await svc.search(SECOND, SearchQuery(trigger="planning", query=repeated["content"]["guidance"], top_k=50))
    assert [h.item["id"] for h in found] == [repeated["id"]]
    # Evaluator cleanup did not disable the production forget rule.
    still_forgotten = fact(SECOND, title=unrelated["content"]["title"], guidance=unrelated["content"]["guidance"])
    assert (await write(svc, SECOND, still_forgotten)).rejected == [f"{still_forgotten['id']}：tombstoned"]


async def test_dedup_and_lineage_cleanup_need_actual_deleted_ids(dev: Backend):
    svc = dev.service
    parent = fact(FIRST, title="Orchid cache setting", guidance="Orchid cache stores seven entries.")
    child = fact(FIRST, title="Cobalt transport protocol", guidance="Cobalt transport uses binary framing.", lineage=[parent["id"]])
    assert (await write(svc, FIRST, parent, child)).accepted == [parent["id"], child["id"]]
    duplicate = fact(FIRST, title=child["content"]["title"], guidance=child["content"]["guidance"], lineage=[parent["id"]])
    merged = await write(svc, FIRST, duplicate)
    assert merged.merged == [child["id"]] and merged.accepted == []
    assert await svc.get(FIRST, duplicate["id"]) is None

    deleted = await svc.delete(FIRST, parent["id"])
    assert set(deleted) == {parent["id"], child["id"]}
    assert duplicate["id"] not in deleted
    assert await stored_ids(dev, FIRST, "memory_items") == set()
    assert await stored_ids(dev, FIRST, "memory_lineage") == set()
    assert await dev.vectors.bind(FIRST.tenant_context()).get(CORPUS, [*deleted, duplicate["id"]]) == []

    # A duplicate's requested ID never identifies the tombstone of its survivor.
    await clear_recorded_test_tombstones(dev, FIRST, [duplicate["id"]])
    assert await stored_ids(dev, FIRST) == set(deleted)
    await clear_recorded_test_tombstones(dev, FIRST, deleted)
    assert await stored_ids(dev, FIRST) == set()
    again_parent = fact(SECOND, title=parent["content"]["title"], guidance=parent["content"]["guidance"])
    again_child = fact(SECOND, title=child["content"]["title"], guidance=child["content"]["guidance"], lineage=[again_parent["id"]])
    accepted = await write(svc, SECOND, again_parent, again_child)
    assert accepted.accepted == [again_parent["id"], again_child["id"]] and not accepted.rejected
    assert [h.item["id"] for h in await svc.search(SECOND, SearchQuery(trigger="planning", query=again_child["content"]["guidance"]))] == [again_child["id"]]


async def test_pending_queue_or_live_items_and_other_tenant_cannot_be_cleaned(dev: Backend):
    svc = dev.service
    pending = fact(FIRST)
    assert await svc.submit(FIRST, [(pending, TASK_ID)]) == 1
    with pytest.raises(ValueError, match="drain"):
        await clear_recorded_test_tombstones(dev, FIRST, [pending["id"]])
    await svc.drain(FIRST)
    with pytest.raises(ValueError, match="live items"):
        await clear_recorded_test_tombstones(dev, FIRST, [pending["id"]])
    with pytest.raises(ValueError, match="isolated"):
        await clear_recorded_test_tombstones(dev, OTHER, [pending["id"]])
    assert await svc.get(FIRST, pending["id"]) is not None
