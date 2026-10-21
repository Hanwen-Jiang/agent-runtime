"""按租户的数据密钥（附录 I.7“存储按租户分区并使用独立密钥”；计划 S-12b）。

每个作用域、每个用途一把密钥：第一次用时向 SecretStore 要一把数据密钥（generate_data_key，按租户包装），
把包装后的形式存进 RelationalStore 的 agentruntime_keyring 表（按作用域隔离：开发实现每个作用域一个库，
生产实现是行级安全）；之后都从表里取出包装后的形式再解开。明文密钥不落盘，只缓存在进程内。

并发的第一次使用由唯一约束裁决（INSERT … ON CONFLICT DO NOTHING 之后再读一次），大家拿到的是同一把。
用途（purpose）例如 vector_transform（向量的保距变换）、memory_body（记忆条目正文的加密）。
"""

from __future__ import annotations

import asyncio
import base64
from typing import Protocol

from agentruntime_core.providers.relational_store import Migration, RelationalStore, RelationalStoreProvider
from agentruntime_core.providers.secret_store import SecretStore, SecretStoreProvider
from agentruntime_core.tenancy import Scope

MIGRATIONS = (
    Migration(
        version=900,
        name="agentruntime_keyring",
        statements=("CREATE TABLE agentruntime_keyring (purpose TEXT PRIMARY KEY, key_id TEXT NOT NULL, wrapped TEXT NOT NULL)",),
    ),
)
_SELECT = "SELECT key_id, wrapped FROM agentruntime_keyring WHERE purpose = $1"
_INSERT = "INSERT INTO agentruntime_keyring (purpose, key_id, wrapped) VALUES ($1, $2, $3) ON CONFLICT (purpose) DO NOTHING"


class KeySource(Protocol):
    async def key(self, scope: Scope, purpose: str) -> bytes:
        """scope 在 purpose 上的 32 字节密钥；同一作用域、同一用途每次都是同一把。"""
        ...


def _owner(scope: Scope) -> str:
    return f"platform:{scope.purpose}" if scope.is_platform else f"tenant:{scope.tenant_id}"


class RelationalKeyRing:
    def __init__(self, relational: RelationalStoreProvider, secrets: SecretStoreProvider) -> None:
        self.relational = relational
        self.secrets = secrets
        self._migrated = False
        self._cache: dict[tuple[str, str], bytes] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _bind(self, scope: Scope) -> tuple[RelationalStore, SecretStore]:
        if scope.is_platform:
            return self.relational.bind_platform(scope.purpose), self.secrets.bind_platform(scope.purpose)
        tenant = scope.to_tenant_context()
        return self.relational.bind(tenant), self.secrets.bind(tenant)

    async def key(self, scope: Scope, purpose: str) -> bytes:
        purpose = purpose.strip() if isinstance(purpose, str) else ""
        if not purpose:
            raise ValueError("密钥必须写明用途")
        cache_key = (_owner(scope), purpose)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached
            if not self._migrated:
                await self.relational.migrate(MIGRATIONS)
                self._migrated = True
            store, secrets = self._bind(scope)
            async with store.transaction(read_only=True) as tx:
                row = await tx.query_one(_SELECT, (purpose,))
            if row is None:
                generated = await secrets.generate_data_key(purpose)
                async with store.transaction() as tx:
                    await tx.execute(_INSERT, (purpose, generated.key_id, base64.b64encode(generated.wrapped).decode()))
                async with store.transaction(read_only=True) as tx:
                    row = await tx.query_one(_SELECT, (purpose,))
            assert row is not None
            plaintext = await secrets.unwrap_data_key(base64.b64decode(str(row["wrapped"])), purpose=purpose)
            value = plaintext.reveal()
            key = value if isinstance(value, bytes) else value.encode()
            self._cache[cache_key] = key
            return key
