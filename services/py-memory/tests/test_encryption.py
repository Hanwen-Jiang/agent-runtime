"""S-12b：记忆条目的正文按租户密钥加密存储（附录 I.7“存储按租户分区并使用独立密钥”；经人确认的做法：正文 AES-GCM，
可过滤的元数据明文）。向量的保距变换在 Qdrant 适配器里，见 libs/py-core 的 test_qdrant_store。"""

from __future__ import annotations

import sqlite3

import pytest

from .memory_support import ALICE, BOB, Backend, item, write

SECRET_TITLE = "季度预算谈判底线"
SECRET_GUIDANCE = "对供应商报价不要低于九五折的底线"


def raw_sqlite_text(backend: Backend) -> str:
    text = ""
    for path in sorted(backend.relational._directory.glob("*.sqlite3")):
        conn = sqlite3.connect(path)
        try:
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
                for row in conn.execute(f"SELECT * FROM {name}"):
                    text += " ".join(str(v) for v in row) + "\n"
        finally:
            conn.close()
    return text


async def test_item_bodies_are_not_stored_in_plaintext(dev: Backend) -> None:
    entry = item(title=SECRET_TITLE, guidance=SECRET_GUIDANCE)
    await write(dev.service, ALICE, entry)
    raw = raw_sqlite_text(dev)
    assert entry["id"] in raw, "条目在库里"
    assert SECRET_TITLE not in raw and SECRET_GUIDANCE not in raw, "正文（含排队中的候选、修订快照）都不落明文"
    stored = await dev.service.get(ALICE, entry["id"])
    assert stored is not None and stored["content"]["title"] == SECRET_TITLE


async def test_each_tenant_uses_its_own_key(dev: Backend) -> None:
    from agentruntime_core.tenancy import tenant_scope

    a = await dev.service.keyring.key(tenant_scope(ALICE.tenant_context()), "memory_body")
    b = await dev.service.keyring.key(tenant_scope(BOB.tenant_context()), "memory_body")
    assert a != b


@pytest.mark.backends
async def test_postgres_rows_hold_no_plaintext(tmp_path, request) -> None:
    from agentruntime_core.adapters import pgtest

    from .memory_support import make_backend

    async for b in make_backend("prod", tmp_path, request):
        await write(b.service, ALICE, item(title=SECRET_TITLE, guidance=SECRET_GUIDANCE))
        cluster: pgtest.Cluster = request.getfixturevalue("pg_cluster")
        tables = cluster.psql("postgres", b.db, "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()").split()
        dump = "\n".join(cluster.psql("postgres", b.db, f"SELECT * FROM {t}") for t in tables)
        assert SECRET_TITLE not in dump and SECRET_GUIDANCE not in dump
