"""SQLite RelationalStore（开发实现）。语义见 agentruntime_core.providers.relational_store。

每个作用域一个库文件：<目录>/<租户 ID 或 _platform>.sqlite3，租户隔离靠库文件本身。用标准库 sqlite3，
同一作用域的事务在进程内串行（asyncio.Lock），读写都在事件循环线程里进行。
SQL 里的 $1、$2 换成 SQLite 的 ?1、?2；只读事务用 PRAGMA query_only 强制。
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from agentruntime_core.dev._common import scope_id
from agentruntime_core.errors import ConflictError, InvalidError, ProviderError, TransientError
from agentruntime_core.providers.relational_store import Migration, Row
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector

# 字符串字面量原样保留，其余位置的 $N 换成 ?N。
_TOKEN = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\$(\d+)")


def translate(sql: str) -> str:
    return _TOKEN.sub(lambda m: f"?{m.group(1)}" if m.group(1) else m.group(0), sql)


def map_error(error: sqlite3.Error) -> ProviderError:
    text = str(error).lower()
    if isinstance(error, sqlite3.IntegrityError):
        if "unique" in text or "primary key" in text:
            return ConflictError("unique_violation", "唯一约束冲突")
        return InvalidError("constraint_violation", "违反约束")
    if "readonly" in text or "read-only" in text:
        return InvalidError("read_only", "只读事务里不能写入")
    if "locked" in text or "busy" in text:
        return TransientError("busy", "数据库忙")
    return InvalidError("bad_sql", "SQL 不合法：" + str(error).split("\n")[0][:200])


class _Database:
    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()
        self.connection.execute("CREATE TABLE IF NOT EXISTS _migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL)")

    def migrate(self, migrations: Sequence[Migration]) -> None:
        done = {row[0] for row in self.connection.execute("SELECT version FROM _migrations")}
        for migration in sorted(migrations, key=lambda m: m.version):
            if migration.version in done:
                continue
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                for statement in migration.statements:
                    self.connection.execute(statement)
                self.connection.execute("INSERT INTO _migrations (version, name) VALUES (?, ?)", (migration.version, migration.name))
                self.connection.execute("COMMIT")
            except sqlite3.Error as error:
                self.connection.execute("ROLLBACK")
                raise map_error(error) from error


class SQLiteRelationalStoreProvider:
    def __init__(self, directory: Path, *, faults: FaultInjector | None = None) -> None:
        self._directory = Path(directory)
        self._faults = faults or FaultInjector()
        self._migrations: dict[int, Migration] = {}
        self._databases: dict[str, _Database] = {}

    def bind(self, tenant: TenantContext) -> SQLiteRelationalStore:
        return SQLiteRelationalStore(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> SQLiteRelationalStore:
        return SQLiteRelationalStore(self, platform_scope(purpose))

    async def migrate(self, migrations: Sequence[Migration]) -> None:
        for migration in migrations:
            self._migrations[migration.version] = migration
        self._directory.mkdir(parents=True, exist_ok=True)
        for path in self._directory.glob("*.sqlite3"):
            self._database(path.stem)

    async def aclose(self) -> None:
        for database in self._databases.values():
            database.connection.close()
        self._databases.clear()

    def _database(self, owner: str) -> _Database:
        database = self._databases.get(owner)
        if database is None:
            self._directory.mkdir(parents=True, exist_ok=True)
            database = _Database(self._directory / f"{owner}.sqlite3")
            self._databases[owner] = database
        database.migrate(list(self._migrations.values()))
        return database


class _Transaction:
    def __init__(self, connection: sqlite3.Connection, faults: FaultInjector) -> None:
        self._connection = connection
        self._faults = faults

    def _run(self, sql: str, params: Sequence[Any]) -> sqlite3.Cursor:
        try:
            return self._connection.execute(translate(sql), tuple(params))
        except sqlite3.Error as error:
            raise map_error(error) from error

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        self._faults.check("execute")
        return max(self._run(sql, params).rowcount, 0)

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        self._faults.check("query")
        try:
            return [dict(row) for row in self._run(sql, params).fetchall()]
        except sqlite3.Error as error:
            raise map_error(error) from error

    async def query_one(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        self._faults.check("query_one")
        rows = await self.query(sql, params)
        if len(rows) > 1:
            raise InvalidError("too_many_rows", "query_one 得到了多行")
        return rows[0] if rows else None


class SQLiteRelationalStore:
    def __init__(self, provider: SQLiteRelationalStoreProvider, scope: Scope) -> None:
        self._provider = provider
        self._owner = scope_id(scope)

    def transaction(self, *, read_only: bool = False) -> Any:
        return self._transaction(read_only)

    @asynccontextmanager
    async def _transaction(self, read_only: bool) -> AsyncIterator[_Transaction]:
        self._provider._faults.check("transaction")
        database = self._provider._database(self._owner)
        async with database.lock:
            connection = database.connection
            if read_only:
                connection.execute("PRAGMA query_only = ON")
            try:
                connection.execute("BEGIN" if read_only else "BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                connection.execute("PRAGMA query_only = OFF")
                raise map_error(error) from error
            try:
                yield _Transaction(connection, self._provider._faults)
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            else:
                try:
                    connection.execute("COMMIT")
                except sqlite3.Error as error:
                    connection.execute("ROLLBACK")
                    raise map_error(error) from error
            finally:
                if read_only:
                    connection.execute("PRAGMA query_only = OFF")
