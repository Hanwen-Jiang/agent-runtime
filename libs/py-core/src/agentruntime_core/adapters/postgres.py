"""RelationalStore 的 Postgres 适配器：一个库、共享的表，租户隔离靠行级安全（设计书 4.3；计划 S-12b）。

与 Go 的 libs/go-core/adapters/postgres（S-12a）做法一致，语义与开发实现（SQLite）相同，由同一套一致性测试保证：

- 应用 SQL 不写租户条件，也不出现租户列。migrate 执行完每个版本后，给模式里每张表补上 tenant_id 列
  （默认值取会话变量 agentruntime.tenant_id），开启并强制行级安全，建策略 agentruntime_tenant：只看得到、只写得进
  tenant_id 等于会话变量的行；会话变量没设（或为空）时什么都看不到、写不进。表上不允许有别的策略。
- 主键与唯一约束（含唯一索引）改写成以 tenant_id 开头；ON CONFLICT (列…) 的冲突目标随之补上 tenant_id。
- 不支持外键：外键检查会越过行级安全。
- 事务开始时用 set_config(…, true) 设置会话变量，只在本事务内有效；连接归还连接池时再核对一次，不为空就丢弃。
- 应用 SQL 不能碰租户：出现 tenant_id、set_config、current_setting、agentruntime. 或以 SET、RESET 开头的语句 invalid；
  查询结果不带 tenant_id 列。
- 在线服务的数据库角色不能是超级用户，也不能带 BYPASSRLS（connect 时核对，denied）。
- 平台作用域的会话变量取 _platform，与租户 ID 不会冲突。迁移记录在 agentruntime_migrations（不分租户）。

用 psycopg 的同步接口，每次数据库调用放进线程（asyncio.to_thread）：psycopg 的异步模式在 Windows 默认的事件循环上
不能用，而 Temporal 与子进程又需要那个事件循环。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from agentruntime_core.dev._common import scope_id
from agentruntime_core.errors import ConflictError, DeniedError, InvalidError, ProviderError, TransientError
from agentruntime_core.providers.relational_store import Migration, Row
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector

TENANT_SETTING = "agentruntime.tenant_id"
TENANT_COLUMN = "tenant_id"
POLICY_NAME = "agentruntime_tenant"
MIGRATIONS_TABLE = "agentruntime_migrations"

# 策略的条件：会话变量为空等同于没设，任何行都不匹配。
_POLICY_EXPR = "tenant_id = NULLIF(current_setting('agentruntime.tenant_id', true), '')"
# 取不到会话变量时报错，没有租户的写入因此失败。
_COLUMN_DEFAULT = "current_setting('agentruntime.tenant_id')"
_MIGRATE_LOCK = "agentruntime.migrate"
_FORBIDDEN = re.compile(r"(?is)\b(tenant_id|set_config|current_setting)\b|\bagentruntime\.|^\s*(set|reset)\b")
_ON_CONFLICT = re.compile(r"(?is)\bON\s+CONFLICT\s*\(")
# 字符串字面量原样保留，其余位置的 $N 换成命名占位符。
_TOKEN = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\$(\d+)")


@dataclass(frozen=True)
class TableAudit:
    """一张表的行级安全情况（验收“RLS 策略覆盖所有带租户数据的表”的自动核对）。"""

    table: str
    has_tenant_column: bool
    rls_enabled: bool
    rls_forced: bool
    has_policy: bool
    unscoped_unique_keys: tuple[str, ...] = field(default_factory=tuple)

    @property
    def covered(self) -> bool:
        return self.has_tenant_column and self.rls_enabled and self.rls_forced and self.has_policy and not self.unscoped_unique_keys


def map_error(error: psycopg.Error) -> ProviderError:
    """把 Postgres 的错误映射到统一的错误模型。连接类的错误不带原文（里面可能有主机、库名）。"""
    code = getattr(error, "sqlstate", None) or ""
    detail = ""
    diag = getattr(error, "diag", None)
    if code and diag is not None and diag.message_primary:
        detail = f"：{diag.message_primary}"
    if code == "23505":
        return ConflictError("unique_violation", "唯一约束冲突")
    if code.startswith("23"):
        return InvalidError("constraint_violation", "违反约束" + detail)
    if code == "25006":
        return InvalidError("read_only", "只读事务里不能写入")
    if code == "42501":
        return DeniedError("row_level_security", "没有权限，或违反行级安全")
    if code in ("40001", "40P01"):
        return TransientError("serialization_failure", "事务冲突，可重试")
    if not code or code[:2] in ("08", "57", "53"):
        return TransientError("unavailable", "业务库暂时不可用")
    return InvalidError("bad_sql", "SQL 不合法" + detail)


def _prepare(sql: str, params: Sequence[Any]) -> tuple[str, dict[str, Any] | None]:
    """拒绝碰租户的 SQL，给 ON CONFLICT 的冲突目标补上 tenant_id，把 $N 换成 psycopg 的占位符。"""
    if _FORBIDDEN.search(sql):
        raise InvalidError("tenant_sql", "应用 SQL 不能碰租户列、会话变量或会话参数")
    sql = _ON_CONFLICT.sub(lambda m: m.group(0) + TENANT_COLUMN + ", ", sql)
    if not params:
        return sql, None
    escaped = sql.replace("%", "%%")
    return _TOKEN.sub(lambda m: f"%(p{m.group(1)})s" if m.group(1) else m.group(0), escaped), {f"p{i}": v for i, v in enumerate(params, start=1)}


def _discard_if_tenant_left(conn: psycopg.Connection[Any]) -> None:
    """连接归还时核对：租户会话变量不为空（事务没有正常结束等）就抛错，连接池据此丢弃这个连接。"""
    row = conn.execute("SELECT coalesce(current_setting(%s, true), '')", (TENANT_SETTING,)).fetchone()
    if row is None or row[0] != "":
        raise RuntimeError("连接上还留着租户会话变量")


class PostgresRelationalStoreProvider:
    def __init__(self, pool: ConnectionPool[Any], faults: FaultInjector, max_conns: int = 8) -> None:
        self._pool = pool
        self._faults = faults
        # 等连接在事件循环里等，不占线程：线程都卡在取连接上时，拿着连接的事务就没有线程可用了。
        self._slots = asyncio.Semaphore(max(1, max_conns))

    @classmethod
    async def connect(cls, dsn: str, *, max_conns: int = 8, faults: FaultInjector | None = None) -> PostgresRelationalStoreProvider:
        if not dsn:
            raise InvalidError("missing_dsn", "缺少业务库的连接串")
        pool = await asyncio.to_thread(cls._open_pool, dsn, max_conns)
        return cls(pool, faults or FaultInjector(), max_conns)

    @staticmethod
    def _open_pool(dsn: str, max_conns: int) -> ConnectionPool[Any]:
        pool: ConnectionPool[Any] = ConnectionPool(
            dsn, min_size=1, max_size=max(1, max_conns), open=False, kwargs={"autocommit": True}, reset=_discard_if_tenant_left, timeout=10
        )
        try:
            pool.open(wait=True, timeout=10)
        except Exception:  # noqa: BLE001 — 报错里不带连接串
            pool.close()
            raise TransientError("unavailable", "连不上业务库") from None
        try:
            with pool.connection() as conn:
                row = conn.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
        except psycopg.Error:
            pool.close()
            raise TransientError("unavailable", "连不上业务库") from None
        if row is None or row[0] or row[1]:
            pool.close()
            raise DeniedError("role_bypasses_rls", "在线服务的数据库角色不能是超级用户，也不能带 BYPASSRLS")
        return pool

    def bind(self, tenant: TenantContext) -> PostgresRelationalStore:
        return PostgresRelationalStore(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> PostgresRelationalStore:
        return PostgresRelationalStore(self, platform_scope(purpose))

    async def aclose(self) -> None:
        await asyncio.to_thread(self._pool.close)

    # --- 迁移 ---

    async def migrate(self, migrations: Sequence[Migration]) -> None:
        await asyncio.to_thread(self._migrate, list(migrations))

    def _migrate(self, migrations: list[Migration]) -> None:
        try:
            with self._pool.connection() as conn:
                conn.execute(f"CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (version BIGINT PRIMARY KEY, name TEXT NOT NULL)")
                for migration in sorted(migrations, key=lambda m: m.version):
                    with conn.transaction():
                        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_MIGRATE_LOCK,))
                        if conn.execute(f"SELECT 1 FROM {MIGRATIONS_TABLE} WHERE version = %s", (migration.version,)).fetchone() is not None:
                            continue
                        for statement in migration.statements:
                            conn.execute(statement)  # type: ignore[arg-type]
                        self._secure(conn)
                        conn.execute(f"INSERT INTO {MIGRATIONS_TABLE} (version, name) VALUES (%s, %s)", (migration.version, migration.name))
        except psycopg.Error as error:
            raise map_error(error) from error

    @staticmethod
    def _tables(conn: psycopg.Connection[Any]) -> list[tuple[int, str]]:
        rows = conn.execute(
            """SELECT c.oid, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE c.relkind IN ('r', 'p') AND n.nspname = current_schema() AND c.relname <> %s ORDER BY c.relname""",
            (MIGRATIONS_TABLE,),
        ).fetchall()
        return [(int(r[0]), str(r[1])) for r in rows]

    def _secure(self, conn: psycopg.Connection[Any]) -> None:
        row = conn.execute(
            "SELECT count(*) FROM pg_constraint con JOIN pg_namespace n ON n.oid = con.connamespace WHERE con.contype = 'f' AND n.nspname = current_schema()"
        ).fetchone()
        if row and row[0]:
            raise InvalidError("unsupported_schema", "业务库不支持外键：外键检查会越过行级安全")
        for oid, name in self._tables(conn):
            self._secure_table(conn, oid, name)

    def _secure_table(self, conn: psycopg.Connection[Any], oid: int, name: str) -> None:
        table = pgsql.Identifier(name)
        row = conn.execute("SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = %s AND attname = %s AND NOT attisdropped)", (oid, TENANT_COLUMN)).fetchone()
        statements: list[pgsql.Composable] = []
        if not (row and row[0]):
            # 默认值分开设：ADD COLUMN … DEFAULT 会当场求一次默认值（迁移时没有租户会话变量）。
            statements += [
                pgsql.SQL("ALTER TABLE {} ADD COLUMN tenant_id TEXT").format(table),
                pgsql.SQL("ALTER TABLE {} ALTER COLUMN tenant_id SET DEFAULT " + _COLUMN_DEFAULT).format(table),
                pgsql.SQL("ALTER TABLE {} ALTER COLUMN tenant_id SET NOT NULL").format(table),
            ]
        statements += [
            pgsql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(table),
            pgsql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(table),
        ]
        for statement in statements:
            conn.execute(statement)
        policies = [str(r[0]) for r in conn.execute("SELECT polname FROM pg_policy WHERE polrelid = %s", (oid,)).fetchall()]
        for policy in policies:
            if policy != POLICY_NAME:
                raise InvalidError("unsupported_schema", f"表 {name} 上不能有别的行级安全策略（{policy}）")
        if not policies:
            conn.execute(pgsql.SQL("CREATE POLICY {} ON {} USING (" + _POLICY_EXPR + ") WITH CHECK (" + _POLICY_EXPR + ")").format(pgsql.Identifier(POLICY_NAME), table))
        self._scope_unique_keys(conn, oid, name)

    @staticmethod
    def _scope_unique_keys(conn: psycopg.Connection[Any], oid: int, name: str) -> None:
        """把不以 tenant_id 开头的主键、唯一约束与唯一索引改写成以 tenant_id 开头。"""
        rows = conn.execute(
            """SELECT ic.relname, coalesce(con.conname, ''), i.indisprimary,
                   array(SELECT coalesce(a.attname, '') FROM unnest(i.indkey) WITH ORDINALITY k(attnum, ord)
                         LEFT JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum ORDER BY k.ord),
                   coalesce(pg_get_expr(i.indpred, i.indrelid), ''), i.indexprs IS NOT NULL
               FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
               LEFT JOIN pg_constraint con ON con.conindid = i.indexrelid AND con.contype IN ('p', 'u')
               WHERE i.indrelid = %s AND i.indisunique ORDER BY ic.relname""",
            (oid,),
        ).fetchall()
        table = pgsql.Identifier(name)
        for index, constraint, primary, columns, predicate, expression in rows:
            if columns and columns[0] == TENANT_COLUMN:
                continue
            if expression:
                raise InvalidError("unsupported_schema", f"表 {name} 的唯一索引 {index} 含表达式，无法按租户改写")
            cols = pgsql.SQL(", ").join([pgsql.Identifier(TENANT_COLUMN), *(pgsql.Identifier(c) for c in columns)])
            statements: list[pgsql.Composable]
            if constraint and primary:
                statements = [pgsql.SQL("ALTER TABLE {t} DROP CONSTRAINT {c}, ADD CONSTRAINT {c} PRIMARY KEY ({cols})").format(t=table, c=pgsql.Identifier(constraint), cols=cols)]
            elif constraint:
                statements = [pgsql.SQL("ALTER TABLE {t} DROP CONSTRAINT {c}, ADD CONSTRAINT {c} UNIQUE ({cols})").format(t=table, c=pgsql.Identifier(constraint), cols=cols)]
            else:
                create: pgsql.Composable = pgsql.SQL("CREATE UNIQUE INDEX {i} ON {t} ({cols})").format(i=pgsql.Identifier(index), t=table, cols=cols)
                if predicate:
                    create = pgsql.Composed([create, pgsql.SQL(" WHERE "), pgsql.SQL(predicate)])
                statements = [pgsql.SQL("DROP INDEX {}").format(pgsql.Identifier(index)), create]
            for statement in statements:
                conn.execute(statement)

    # --- 核对 ---

    async def audit_rls(self) -> list[TableAudit]:
        return await asyncio.to_thread(self._audit)

    def _audit(self) -> list[TableAudit]:
        try:
            with self._pool.connection() as conn:
                out: list[TableAudit] = []
                for oid, name in self._tables(conn):
                    enabled, forced, has_column, has_policy = conn.execute(  # type: ignore[misc]
                        """SELECT c.relrowsecurity, c.relforcerowsecurity,
                               EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = c.oid AND attname = %s AND attnotnull AND NOT attisdropped),
                               (SELECT count(*) FROM pg_policy WHERE polrelid = c.oid) = 1 AND
                               EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = c.oid AND polname = %s AND polcmd = '*' AND polpermissive
                                       AND pg_get_expr(polqual, polrelid) IS NOT NULL AND pg_get_expr(polwithcheck, polrelid) IS NOT NULL)
                           FROM pg_class c WHERE c.oid = %s""",
                        (TENANT_COLUMN, POLICY_NAME, oid),
                    ).fetchone()
                    unscoped = tuple(
                        str(r[0])
                        for r in conn.execute(
                            """SELECT ic.relname FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
                               LEFT JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
                               WHERE i.indrelid = %s AND i.indisunique AND coalesce(a.attname, '') <> %s ORDER BY 1""",
                            (oid, TENANT_COLUMN),
                        ).fetchall()
                    )
                    out.append(TableAudit(name, bool(has_column), bool(enabled), bool(forced), bool(has_policy), unscoped))
                return out
        except psycopg.Error as error:
            raise map_error(error) from error

    async def connection_tenant_for_test(self) -> str:
        """从连接池借一个连接（不设租户），返回它上面的租户会话变量；供测试核对连接归还时已清除。"""

        def read() -> str:
            with self._pool.connection() as conn:
                row = conn.execute("SELECT coalesce(current_setting(%s, true), '')", (TENANT_SETTING,)).fetchone()
            return str(row[0]) if row else ""

        return await asyncio.to_thread(read)


class _Transaction:
    def __init__(self, conn: psycopg.Connection[Any], faults: FaultInjector) -> None:
        self._conn = conn
        self._faults = faults

    def _run(self, sql: str, params: Sequence[Any], fetch: bool) -> tuple[int, list[dict[str, Any]]]:
        query, args = _prepare(sql, params)
        try:
            with self._conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query, args)  # type: ignore[arg-type]
                rows = cur.fetchall() if fetch and cur.description else []
                return max(cur.rowcount, 0), rows
        except psycopg.Error as error:
            raise map_error(error) from error

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        self._faults.check("execute")
        count, _ = await asyncio.to_thread(self._run, sql, params, False)
        return count

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        self._faults.check("query")
        _, rows = await asyncio.to_thread(self._run, sql, params, True)
        return [{k: v for k, v in row.items() if k != TENANT_COLUMN} for row in rows]

    async def query_one(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        self._faults.check("query_one")
        rows = await self.query(sql, params)
        if len(rows) > 1:
            raise InvalidError("too_many_rows", "query_one 得到了多行")
        return rows[0] if rows else None


class PostgresRelationalStore:
    def __init__(self, provider: PostgresRelationalStoreProvider, scope: Scope) -> None:
        self._provider = provider
        self._owner = scope_id(scope)

    def _begin(self, conn: psycopg.Connection[Any], read_only: bool) -> None:
        conn.execute("BEGIN READ ONLY" if read_only else "BEGIN")
        # 只在本事务内有效（is_local = true）：提交或回滚后自动恢复，连接归还时不带着租户。
        conn.execute("SELECT set_config(%s, %s, true)", (TENANT_SETTING, self._owner))

    @staticmethod
    def _end(conn: psycopg.Connection[Any], statement: str) -> None:
        conn.execute(statement)

    @asynccontextmanager
    async def transaction(self, *, read_only: bool = False) -> AsyncIterator[_Transaction]:
        self._provider._faults.check("transaction")
        async with self._provider._slots:
            async with self._checked_out() as conn:
                try:
                    await asyncio.to_thread(self._begin, conn, read_only)
                except psycopg.Error as error:
                    raise map_error(error) from error
                try:
                    yield _Transaction(conn, self._provider._faults)
                except BaseException:
                    try:
                        await asyncio.shield(asyncio.to_thread(self._end, conn, "ROLLBACK"))
                    except psycopg.Error:
                        pass  # 连接归还时核对会话变量，不干净就丢弃
                    raise
                try:
                    await asyncio.to_thread(self._end, conn, "COMMIT")
                except psycopg.Error as error:
                    raise map_error(error) from error

    @asynccontextmanager
    async def _checked_out(self) -> AsyncIterator[psycopg.Connection[Any]]:
        pool = self._provider._pool
        try:
            conn = await asyncio.to_thread(pool.getconn)
        except PoolTimeout:
            raise TransientError("unavailable", "业务库的连接池已满或连不上") from None
        try:
            yield conn
        finally:
            await asyncio.shield(asyncio.to_thread(pool.putconn, conn))
