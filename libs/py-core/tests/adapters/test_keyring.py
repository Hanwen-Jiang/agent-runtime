"""S-12b：按租户的数据密钥（附录 I.7“存储按租户分区并使用独立密钥”）。

密钥由 SecretStore 生成并按租户包装，包装后的形式存在 RelationalStore 里（按作用域隔离）；明文不落盘。
"""

from __future__ import annotations

import asyncio
import base64
import sqlite3
from pathlib import Path

import pytest

from agentruntime_core.adapters.keyring import RelationalKeyRing
from agentruntime_core.conformance.common import tenant_a, tenant_b
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.tenancy import platform_scope, tenant_scope


def keyring(tmp_path: Path) -> RelationalKeyRing:
    return RelationalKeyRing(SQLiteRelationalStoreProvider(tmp_path / "db"), LocalFileSecretStoreProvider(tmp_path / "secrets.json"))


async def test_the_same_scope_and_purpose_always_get_the_same_key(tmp_path: Path) -> None:
    ring = keyring(tmp_path)
    a = tenant_scope(tenant_a())
    first = await ring.key(a, "vector_transform")
    assert len(first) == 32
    assert await ring.key(a, "vector_transform") == first
    assert await keyring(tmp_path).key(a, "vector_transform") == first, "换一个实例（例如重启）仍是同一把"


async def test_keys_differ_per_tenant_purpose_and_platform(tmp_path: Path) -> None:
    ring = keyring(tmp_path)
    a, b = tenant_scope(tenant_a()), tenant_scope(tenant_b())
    keys = {
        await ring.key(a, "vector_transform"),
        await ring.key(b, "vector_transform"),
        await ring.key(a, "memory_body"),
        await ring.key(platform_scope("memory"), "vector_transform"),
    }
    assert len(keys) == 4


async def test_concurrent_first_use_settles_on_one_key(tmp_path: Path) -> None:
    ring = keyring(tmp_path)
    a = tenant_scope(tenant_a())
    keys = await asyncio.gather(*(ring.key(a, "vector_transform") for _ in range(10)))
    assert len(set(keys)) == 1


async def test_only_the_wrapped_key_is_stored(tmp_path: Path) -> None:
    ring = keyring(tmp_path)
    key = await ring.key(tenant_scope(tenant_a()), "memory_body")
    stored = b""
    for db in (tmp_path / "db").glob("*.sqlite3"):
        conn = sqlite3.connect(db)
        try:
            for (wrapped,) in conn.execute("SELECT wrapped FROM agentruntime_keyring"):
                stored += wrapped.encode()
        finally:
            conn.close()
    assert stored, "包装后的密钥存在 RelationalStore 里"
    assert key.hex().encode() not in stored and key not in stored
    assert base64.b64encode(key) not in stored


@pytest.mark.parametrize("purpose", ["", "  "])
async def test_purpose_is_required(tmp_path: Path, purpose: str) -> None:
    with pytest.raises(ValueError):
        await keyring(tmp_path).key(tenant_scope(tenant_a()), purpose)
