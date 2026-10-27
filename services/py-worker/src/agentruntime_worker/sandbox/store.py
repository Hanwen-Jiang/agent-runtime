"""沙箱环境表用的业务库（RelationalStore）：设了 AR_RELATIONAL_DSN 时是 Postgres（行级安全，与 task-service 同一个库、同一个
连接角色），否则是数据目录下的 SQLite（<数据目录>/relational/<作用域>.sqlite3，与 task-service、包镜像、出网网关的布局相同）。

Postgres 的连接池要 await 才能建，工具又是同步登记的，所以第一次用到时才连（LazyRelationalStore）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from agentruntime_core.providers.relational_store import Migration, RelationalStoreProvider, Transaction
from agentruntime_core.tenancy import TenantContext


class LazyRelationalStore:
    """第一次用到时才建的 RelationalStoreProvider（同一个实例只建一次）。"""

    def __init__(self, factory: Callable[[], Awaitable[RelationalStoreProvider]]) -> None:
        self._factory = factory
        self._provider: RelationalStoreProvider | None = None
        self._lock = asyncio.Lock()

    async def provider(self) -> RelationalStoreProvider:
        if self._provider is None:
            async with self._lock:
                if self._provider is None:
                    self._provider = await self._factory()
        return self._provider

    async def migrate(self, migrations: Sequence[Migration]) -> None:
        await (await self.provider()).migrate(migrations)

    def bind(self, tenant: TenantContext) -> _LazyBound:
        return _LazyBound(self, tenant, "")

    def bind_platform(self, purpose: str) -> _LazyBound:
        return _LazyBound(self, None, purpose)

    async def aclose(self) -> None:
        if self._provider is not None:
            close = getattr(self._provider, "aclose", None)
            if close is not None:
                await close()
            self._provider = None


class _LazyBound:
    def __init__(self, lazy: LazyRelationalStore, tenant: TenantContext | None, purpose: str) -> None:
        self._lazy = lazy
        self._tenant = tenant
        self._purpose = purpose

    def transaction(self, *, read_only: bool = False) -> Any:
        return self._transaction(read_only)

    @asynccontextmanager
    async def _transaction(self, read_only: bool) -> AsyncIterator[Transaction]:
        provider = await self._lazy.provider()
        store = provider.bind(self._tenant) if self._tenant is not None else provider.bind_platform(self._purpose)
        async with store.transaction(read_only=read_only) as tx:
            yield tx


def relational_store(dsn: str, data_dir: Path) -> LazyRelationalStore:
    async def build() -> RelationalStoreProvider:
        if dsn:
            from agentruntime_core.adapters.postgres import PostgresRelationalStoreProvider

            return await PostgresRelationalStoreProvider.connect(dsn)
        from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider

        return SQLiteRelationalStoreProvider(Path(data_dir) / "relational")

    return LazyRelationalStore(build)
