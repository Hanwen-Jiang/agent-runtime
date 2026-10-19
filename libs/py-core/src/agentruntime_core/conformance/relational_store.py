"""RelationalStore 的一致性测试。语义见 agentruntime_core.providers.relational_store。"""

from __future__ import annotations

import asyncio

import pytest

from agentruntime_core.conformance.common import Harness, StandardRules, expect_error, tenant, tenant_a
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.providers.relational_store import Migration, RelationalStore, RelationalStoreProvider

MIGRATIONS = (
    Migration(
        version=1,
        name="conformance_items",
        statements=(
            "CREATE TABLE conformance_items ("
            " id TEXT PRIMARY KEY,"
            " name TEXT NOT NULL UNIQUE,"
            " qty BIGINT NOT NULL DEFAULT 0,"
            " price DOUBLE PRECISION,"
            " note TEXT)",
        ),
    ),
)

INSERT = "INSERT INTO conformance_items (id, name, qty, price, note) VALUES ($1, $2, $3, $4, $5)"

RelationalHarness = Harness[RelationalStoreProvider]


class UserError(Exception):
    """业务代码自己的异常，事务回滚后应原样抛出。"""


class RelationalStoreConformance(StandardRules):
    probe_operation = "execute"

    async def write_probe(self, client: RelationalStore, name: str) -> None:
        async with client.transaction() as tx:
            await tx.execute(INSERT, (name, name, 1, None, None))

    async def probe_visible(self, client: RelationalStore, name: str) -> bool:
        async with client.transaction(read_only=True) as tx:
            return await tx.query_one("SELECT id FROM conformance_items WHERE id = $1", (name,)) is not None

    @pytest.fixture(autouse=True)
    async def _schema(self, harness: RelationalHarness) -> None:
        await harness.provider.migrate(MIGRATIONS)

    @pytest.fixture
    def store(self, harness: RelationalHarness) -> RelationalStore:
        return harness.provider.bind(tenant_a())

    async def _count(self, store: RelationalStore) -> int:
        async with store.transaction(read_only=True) as tx:
            row = await tx.query_one("SELECT COUNT(*) AS n FROM conformance_items")
        assert row is not None
        return int(row["n"])

    async def test_commit_makes_rows_visible(self, store: RelationalStore) -> None:
        async with store.transaction() as tx:
            assert await tx.execute(INSERT, ("i1", "widget", 3, 9.5, "备注")) == 1
        async with store.transaction(read_only=True) as tx:
            row = await tx.query_one("SELECT id, name, qty, price, note FROM conformance_items WHERE id = $1", ("i1",))
        assert row is not None
        assert dict(row) == {"id": "i1", "name": "widget", "qty": 3, "price": 9.5, "note": "备注"}

    async def test_exception_rolls_back_and_propagates(self, store: RelationalStore) -> None:
        with pytest.raises(UserError):
            async with store.transaction() as tx:
                await tx.execute(INSERT, ("i1", "widget", 1, None, None))
                raise UserError("业务失败")
        assert await self._count(store) == 0

    async def test_portable_types_roundtrip(self, store: RelationalStore) -> None:
        big = 2**53 + 1
        async with store.transaction() as tx:
            await tx.execute(INSERT, ("i1", "名称 ✓", big, -0.125, None))
        async with store.transaction(read_only=True) as tx:
            row = await tx.query_one("SELECT name, qty, price, note FROM conformance_items WHERE id = $1", ("i1",))
        assert row is not None
        assert row["name"] == "名称 ✓"
        assert row["qty"] == big and isinstance(row["qty"], int)
        assert row["price"] == -0.125 and isinstance(row["price"], float)
        assert row["note"] is None

    async def test_positional_parameters_can_be_reordered(self, store: RelationalStore) -> None:
        async with store.transaction() as tx:
            await tx.execute(INSERT, ("i1", "widget", 5, None, None))
            rows = await tx.query("SELECT id FROM conformance_items WHERE name = $2 AND qty = $1", (5, "widget"))
        assert [r["id"] for r in rows] == ["i1"]

    async def test_query_returns_rows_in_requested_order(self, store: RelationalStore) -> None:
        async with store.transaction() as tx:
            for i, name in enumerate(["c", "a", "b"]):
                await tx.execute(INSERT, (f"i{i}", name, i, None, None))
            rows = await tx.query("SELECT name FROM conformance_items ORDER BY name")
        assert [r["name"] for r in rows] == ["a", "b", "c"]

    async def test_query_one_with_several_rows_is_invalid(self, store: RelationalStore) -> None:
        async with store.transaction() as tx:
            await tx.execute(INSERT, ("i1", "a", 1, None, None))
            await tx.execute(INSERT, ("i2", "b", 1, None, None))
        with expect_error(ErrorCategory.INVALID):
            async with store.transaction(read_only=True) as tx:
                await tx.query_one("SELECT id FROM conformance_items")

    async def test_unique_violation_is_conflict(self, store: RelationalStore) -> None:
        async with store.transaction() as tx:
            await tx.execute(INSERT, ("i1", "widget", 1, None, None))
        with expect_error(ErrorCategory.CONFLICT):
            async with store.transaction() as tx:
                await tx.execute(INSERT, ("i2", "widget", 1, None, None))
        assert await self._count(store) == 1

    @pytest.mark.parametrize(
        "sql",
        ["SELEC id FROM conformance_items", "SELECT id FROM no_such_table", "SELECT no_such_column FROM conformance_items"],
        ids=["syntax", "missing_table", "missing_column"],
    )
    async def test_bad_sql_is_invalid(self, store: RelationalStore, sql: str) -> None:
        with expect_error(ErrorCategory.INVALID):
            async with store.transaction(read_only=True) as tx:
                await tx.query(sql)

    async def test_write_in_read_only_transaction_is_invalid(self, store: RelationalStore) -> None:
        with expect_error(ErrorCategory.INVALID):
            async with store.transaction(read_only=True) as tx:
                await tx.execute(INSERT, ("i1", "widget", 1, None, None))
        assert await self._count(store) == 0

    async def test_upsert_is_idempotent(self, store: RelationalStore) -> None:
        upsert_ignore = INSERT + " ON CONFLICT (id) DO NOTHING"
        async with store.transaction() as tx:
            assert await tx.execute(upsert_ignore, ("i1", "widget", 1, None, None)) == 1
            assert await tx.execute(upsert_ignore, ("i1", "widget", 1, None, None)) == 0
        upsert_update = INSERT + " ON CONFLICT (id) DO UPDATE SET qty = excluded.qty"
        async with store.transaction() as tx:
            await tx.execute(upsert_update, ("i1", "widget", 7, None, None))
            row = await tx.query_one("SELECT qty FROM conformance_items WHERE id = $1", ("i1",))
        assert row is not None and row["qty"] == 7
        assert await self._count(store) == 1

    async def test_delete_removes_rows(self, store: RelationalStore) -> None:
        async with store.transaction() as tx:
            await tx.execute(INSERT, ("i1", "widget", 1, None, None))
        async with store.transaction() as tx:
            assert await tx.execute("DELETE FROM conformance_items WHERE id = $1", ("i1",)) == 1
            assert await tx.execute("DELETE FROM conformance_items WHERE id = $1", ("i1",)) == 0
        assert await self._count(store) == 0

    async def test_concurrent_increments_are_not_lost(self, store: RelationalStore) -> None:
        async with store.transaction() as tx:
            await tx.execute(INSERT, ("i1", "counter", 0, None, None))

        async def bump() -> None:
            async with store.transaction() as tx:
                await tx.execute("UPDATE conformance_items SET qty = qty + 1 WHERE id = $1", ("i1",))

        await asyncio.gather(*(bump() for _ in range(20)))
        async with store.transaction(read_only=True) as tx:
            row = await tx.query_one("SELECT qty FROM conformance_items WHERE id = $1", ("i1",))
        assert row is not None and row["qty"] == 20

    async def test_migrate_is_idempotent_and_covers_new_tenants(self, harness: RelationalHarness) -> None:
        await harness.provider.migrate(MIGRATIONS)
        late = harness.provider.bind(tenant("t-gamma"))
        async with late.transaction() as tx:
            await tx.execute(INSERT, ("i1", "widget", 1, None, None))
        assert await self._count(late) == 1

    async def test_other_tenant_writes_do_not_collide(self, harness: RelationalHarness, store: RelationalStore) -> None:
        """同一主键在两个租户里各写一行，互不冲突，也互不可见。"""
        other = harness.provider.bind(tenant("t-beta"))
        async with store.transaction() as tx:
            await tx.execute(INSERT, ("i1", "widget", 1, None, None))
        async with other.transaction() as tx:
            await tx.execute(INSERT, ("i1", "widget", 2, None, None))
        async with other.transaction(read_only=True) as tx:
            row = await tx.query_one("SELECT qty FROM conformance_items WHERE id = $1", ("i1",))
        assert row is not None and row["qty"] == 2
        assert await self._count(store) == 1
