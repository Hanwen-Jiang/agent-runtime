"""S-12b：RelationalStore 的 Postgres 适配器（Python 侧；做法与 Go 的 S-12a 一致，设计书 4.3；计划 S-12 难点“RLS 与连接池”）。

- 与开发实现同一套一致性测试（含租户绑定与隔离规则）。
- 迁移出来的每张表都有租户列、开启并强制了行级安全、有按会话变量过滤的策略，唯一约束以租户开头。
- 缺少租户条件的查询什么都看不到、写不进；应用 SQL 改不了租户；查询结果不带租户列。
- 会话变量只在事务内有效，连接归还时清除；并发的不同租户互不可见。
- 在线服务的角色不能绕过行级安全；外键不受支持。

用临时的 Postgres 实例（pgtest，二进制取自 PATH 或 AR_POSTGRES_BIN），核对用的原始 SQL 经 psql 执行。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from agentruntime_core.adapters import pgtest
from agentruntime_core.adapters.postgres import POLICY_NAME, PostgresRelationalStoreProvider
from agentruntime_core.conformance.common import expect_error, tenant, tenant_a, tenant_b
from agentruntime_core.conformance.relational_store import RelationalStoreConformance
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.providers.relational_store import Migration
from agentruntime_core.testing import FaultInjector

# 版本号接在一致性测试的迁移（版本 1）后面。
SCHEMAS = (
    Migration(version=10, name="tasks", statements=("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL, UNIQUE (session_id, task_id))",)),
    Migration(version=11, name="events", statements=("CREATE TABLE events (seq BIGINT NOT NULL, task_id TEXT NOT NULL, body TEXT)", "CREATE UNIQUE INDEX events_task_seq ON events (task_id, seq)")),
    Migration(version=12, name="tasks_outcome", statements=("ALTER TABLE tasks ADD COLUMN outcome TEXT",)),
)


class Harness:
    def __init__(self, provider: PostgresRelationalStoreProvider, faults: FaultInjector) -> None:
        self.provider = provider
        self.faults = faults

    async def advance(self, delta: timedelta) -> None:
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)


@pytest.fixture
def database(pg_cluster: pgtest.Cluster):
    db = pg_cluster.new_database()
    yield db
    pg_cluster.drop_database(db)


@pytest.fixture
async def provider(pg_cluster: pgtest.Cluster, database: str):
    p = await PostgresRelationalStoreProvider.connect(pg_cluster.dsn(pgtest.APP_ROLE, database), max_conns=4)
    yield p
    await p.aclose()


class TestPostgresRelationalStore(RelationalStoreConformance):
    @pytest.fixture
    async def harness(self, pg_cluster: pgtest.Cluster, database: str):
        faults = FaultInjector()
        p = await PostgresRelationalStoreProvider.connect(pg_cluster.dsn(pgtest.APP_ROLE, database), max_conns=4, faults=faults)
        yield Harness(p, faults)
        await p.aclose()


async def test_every_migrated_table_is_covered_by_row_level_security(provider: PostgresRelationalStoreProvider, pg_cluster: pgtest.Cluster, database: str) -> None:
    await provider.migrate(SCHEMAS)
    audits = {a.table: a for a in await provider.audit_rls()}
    assert set(audits) >= {"tasks", "events"}
    assert all(a.covered for a in audits.values()), audits
    # 不经适配器再核对一次：pg_class 的标志与策略。
    rows = pg_cluster.psql("postgres", database, "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname IN ('tasks', 'events') ORDER BY relname")
    assert rows.splitlines() == ["events|t|t", "tasks|t|t"]
    policies = pg_cluster.psql("postgres", database, "SELECT DISTINCT polname FROM pg_policy")
    assert policies.splitlines() == [POLICY_NAME]


async def test_migrating_twice_is_harmless(provider: PostgresRelationalStoreProvider) -> None:
    await provider.migrate(SCHEMAS)
    await provider.migrate(SCHEMAS)
    assert all(a.covered for a in await provider.audit_rls())


async def test_queries_without_the_tenant_setting_see_nothing_and_cannot_write(provider: PostgresRelationalStoreProvider, pg_cluster: pgtest.Cluster, database: str) -> None:
    await provider.migrate(SCHEMAS)
    async with provider.bind(tenant_a()).transaction() as tx:
        await tx.execute("INSERT INTO tasks (task_id, session_id, status) VALUES ($1, $2, $3)", ("t1", "s1", "running"))
    assert pg_cluster.psql(pgtest.APP_ROLE, database, "SELECT count(*) FROM tasks") == "0", "没有租户会话变量什么都看不到"
    with pytest.raises(RuntimeError):
        pg_cluster.psql(pgtest.APP_ROLE, database, "INSERT INTO tasks (task_id, session_id, status) VALUES ('t2', 's', 'x')")


async def test_a_tenant_cannot_read_or_change_another_tenants_rows(provider: PostgresRelationalStoreProvider) -> None:
    await provider.migrate(SCHEMAS)
    a, b = provider.bind(tenant_a()), provider.bind(tenant_b())
    async with a.transaction() as tx:
        await tx.execute("INSERT INTO tasks (task_id, session_id, status) VALUES ($1, $2, $3)", ("t1", "s1", "running"))
    async with b.transaction() as tx:
        assert await tx.query("SELECT * FROM tasks") == []
        assert await tx.execute("UPDATE tasks SET status = 'hacked'") == 0
        assert await tx.execute("DELETE FROM tasks") == 0
        # 同一个主键在别的租户下互不冲突，冲突目标自动按租户。
        await tx.execute("INSERT INTO tasks (task_id, session_id, status) VALUES ($1, $2, $3)", ("t1", "s9", "queued"))
        await tx.execute(
            "INSERT INTO tasks (task_id, session_id, status) VALUES ($1, $2, $3) ON CONFLICT (task_id) DO UPDATE SET status = excluded.status",
            ("t1", "s9", "done"),
        )
    async with a.transaction(read_only=True) as tx:
        row = await tx.query_one("SELECT status, session_id FROM tasks WHERE task_id = $1", ("t1",))
    assert row is not None and dict(row) == {"status": "running", "session_id": "s1"}


async def test_query_results_never_carry_the_tenant_column(provider: PostgresRelationalStoreProvider) -> None:
    await provider.migrate(SCHEMAS)
    async with provider.bind(tenant_a()).transaction() as tx:
        await tx.execute("INSERT INTO tasks (task_id, session_id, status) VALUES ($1, $2, $3)", ("t1", "s1", "running"))
        [row] = await tx.query("SELECT * FROM tasks")
    assert "tenant_id" not in row


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT tenant_id FROM tasks",
        "UPDATE tasks SET tenant_id = 't-beta'",
        "SELECT set_config('agentruntime.tenant_id', 't-beta', true)",
        "SELECT current_setting('agentruntime.tenant_id')",
        "SET agentruntime.tenant_id = 't-beta'",
        "RESET agentruntime.tenant_id",
        "set role postgres",
    ],
)
async def test_application_sql_cannot_touch_the_tenant(provider: PostgresRelationalStoreProvider, sql: str) -> None:
    await provider.migrate(SCHEMAS)
    with expect_error(ErrorCategory.INVALID):
        async with provider.bind(tenant_a()).transaction() as tx:
            await tx.execute(sql)


async def test_the_tenant_setting_does_not_outlive_the_transaction(provider: PostgresRelationalStoreProvider) -> None:
    await provider.migrate(SCHEMAS)
    async with provider.bind(tenant_a()).transaction() as tx:
        await tx.query("SELECT * FROM tasks")
    assert await provider.connection_tenant_for_test() == ""

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        async with provider.bind(tenant_b()).transaction() as tx:
            await tx.query("SELECT * FROM tasks")
            raise Boom
    assert await provider.connection_tenant_for_test() == ""


async def test_concurrent_tenants_never_see_each_other(pg_cluster: pgtest.Cluster, database: str) -> None:
    """连接池只有两个连接，十几个不同租户的事务交错执行：每个只看得到自己的行。"""
    p = await PostgresRelationalStoreProvider.connect(pg_cluster.dsn(pgtest.APP_ROLE, database), max_conns=2)
    try:
        await p.migrate(SCHEMAS)
        tenants = [tenant(f"t-{i}") for i in range(12)]

        async def work(i: int) -> list[str]:
            store = p.bind(tenants[i])
            async with store.transaction() as tx:
                await tx.execute("INSERT INTO tasks (task_id, session_id, status) VALUES ($1, $2, $3)", (f"task-{i}", "s", "running"))
                await asyncio.sleep(0.01)
            async with store.transaction(read_only=True) as tx:
                return [str(r["task_id"]) for r in await tx.query("SELECT task_id FROM tasks")]

        results = await asyncio.gather(*(work(i) for i in range(12)))
        assert results == [[f"task-{i}"] for i in range(12)]
    finally:
        await p.aclose()


async def test_foreign_keys_are_rejected(provider: PostgresRelationalStoreProvider) -> None:
    fk = Migration(
        version=20,
        name="fk",
        statements=("CREATE TABLE parents (id TEXT PRIMARY KEY)", "CREATE TABLE children (id TEXT PRIMARY KEY, parent TEXT REFERENCES parents (id))"),
    )
    with expect_error(ErrorCategory.INVALID):
        await provider.migrate([fk])


async def test_the_online_role_must_not_bypass_row_level_security(pg_cluster: pgtest.Cluster, database: str) -> None:
    with expect_error(ErrorCategory.DENIED):
        await PostgresRelationalStoreProvider.connect(pg_cluster.dsn("postgres", database))
    pg_cluster.ensure_role("agentruntime_bypass", "NOSUPERUSER BYPASSRLS")
    with expect_error(ErrorCategory.DENIED):
        await PostgresRelationalStoreProvider.connect(pg_cluster.dsn("agentruntime_bypass", database))


async def test_errors_do_not_reveal_the_connection_string(pg_cluster: pgtest.Cluster) -> None:
    dsn = pg_cluster.dsn(pgtest.APP_ROLE, "no_such_database_x9")
    with pytest.raises(Exception) as err:
        await PostgresRelationalStoreProvider.connect(dsn)
    assert dsn not in str(err.value) and "no_such_database_x9" not in str(err.value)


async def test_platform_scope_is_separate_from_tenants(provider: PostgresRelationalStoreProvider) -> None:
    await provider.migrate(SCHEMAS)
    async with provider.bind_platform("ops").transaction() as tx:
        await tx.execute("INSERT INTO tasks (task_id, session_id, status) VALUES ($1, $2, $3)", ("p1", "s", "x"))
    async with provider.bind(tenant_a()).transaction(read_only=True) as tx:
        assert await tx.query("SELECT * FROM tasks") == []
    async with provider.bind_platform("ops").transaction(read_only=True) as tx:
        assert len(await tx.query("SELECT * FROM tasks")) == 1
