"""Memory Service 测试共用：身份、条目构造与两套后端。

后端：
- dev：SQLite（每个作用域一个库）+ 内存 VectorStore；
- prod：Postgres（行级安全）+ Qdrant（租户字段分区、强制过滤、保距变换），二进制取自 PATH 或 AR_POSTGRES_BIN、AR_QDRANT_BIN。
密钥环都放在同一个 RelationalStore 里，数据密钥由开发版 SecretStore 生成。嵌入用确定的 HashingEmbedder。
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.adapters import pgtest, qdranttest
from agentruntime_core.adapters.keyring import RelationalKeyRing
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.dev.vector_store import InMemoryVectorStoreProvider
from agentruntime_core.testing import FakeClock

from agentruntime_memory.embedding import HashingEmbedder
from agentruntime_memory.identity import Identity
from agentruntime_memory.service import MemoryService

T_A = "t-alpha"
T_B = "t-beta"
SESSION_1 = "ses_01J" + "0" * 22 + "A"
SESSION_2 = "ses_01J" + "0" * 22 + "B"
SESSION_9 = "ses_01J" + "0" * 22 + "Z"
TASK_ID = "tsk_01J" + "0" * 22 + "M"
NOW = datetime(2026, 10, 1, tzinfo=UTC)

ALICE = Identity(tenant_id=T_A, user_id="u-alice", workspace_id="w-1", session_id=SESSION_1)
CAROL = Identity(tenant_id=T_A, user_id="u-carol", workspace_id="w-1", session_id=SESSION_2)
DAVE = Identity(tenant_id=T_A, user_id="u-dave", workspace_id="w-2")
MAINTAINER = Identity(tenant_id=T_A, user_id="u-maint", workspace_id="w-1", roles=frozenset({"maintainer"}))
ADMIN = Identity(tenant_id=T_A, user_id="u-admin", workspace_id="w-1", roles=frozenset({"admin"}))
REVIEWER = Identity(tenant_id=T_A, user_id="u-plat", roles=frozenset({"platform_reviewer"}))
BOB = Identity(tenant_id=T_B, user_id="u-bob", workspace_id="w-9", session_id=SESSION_9)

_ids = itertools.count(1)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id() -> str:
    n = next(_ids)
    tail = ""
    for _ in range(25):
        tail = _CROCKFORD[n % 32] + tail
        n //= 32
    return "mem_0" + tail


def scope(identity: Identity, level: str) -> dict[str, Any]:
    """identity 在 level 层级上的作用域（memory_item.scope）。"""
    base: dict[str, Any] = {"tenant": identity.tenant_id, "workspace": None, "user": None, "session": None, "task": None}
    if level == "platform":
        return {"tenant": None, "workspace": None, "user": None, "session": None, "task": None}
    if level == "workspace":
        base["workspace"] = identity.workspace_id
    elif level == "user":
        base["user"] = identity.user_id
    elif level == "user_workspace":
        base["user"], base["workspace"] = identity.user_id, identity.workspace_id
    elif level == "session":
        base["user"], base["session"] = identity.user_id, identity.session_id
    elif level == "task":
        base["user"], base["session"], base["task"] = identity.user_id, identity.session_id, TASK_ID
    return base


def item(
    identity: Identity = ALICE,
    *,
    level: str = "session",
    kind: str = "failure",
    title: str = "测试依赖本地 Redis",
    guidance: str = "运行 make test 前先执行 docker compose up -d redis",
    task_family: tuple[str, ...] = ("engineering.ci_fix",),
    tools: tuple[str, ...] = ("docker",),
    source: str = "agent_reflection",
    trust: float = 0.6,
    evidence: list[dict[str, str]] | None = None,
    valid_from: str = "2026-09-01",
    valid_to: str | None = None,
    lineage: list[str] | None = None,
    id: str | None = None,
    sensitivity: str = "none",
) -> dict[str, Any]:
    """一条合乎 memory_item.schema.json 的条目。"""
    out: dict[str, Any] = {
        "id": id or new_id(),
        "scope": scope(identity, level),
        "kind": kind,
        "applies_when": {"task_family": list(task_family), "tools": list(tools), "model_family": "any"},
        "content": {"title": title, "guidance": guidance},
        "provenance": {"source": source},
        "trust": trust,
        "validity": {"valid_from": valid_from, "valid_to": valid_to, "superseded_by": None},
        "sensitivity": sensitivity,
        "embedding_version": "pending",
        "version": 1,
    }
    if evidence:
        out["evidence"] = evidence
    if lineage:
        out["lineage"] = lineage
    return out


def verified(n: int) -> list[dict[str, str]]:
    return [{"trace": "tr_" + uuid.UUID(int=i + 1).hex, "outcome": "verified"} for i in range(n)]


@dataclass
class Backend:
    name: str
    service: MemoryService
    relational: Any
    vectors: Any
    clock: FakeClock
    db: str = ""
    """生产后端的 Postgres 库名（核对存储层用）。"""


async def make_backend(name: str, tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[Backend]:
    clock = FakeClock(NOW)
    secrets = LocalFileSecretStoreProvider(tmp_path / "secrets.json")
    closers = []
    db = ""
    if name == "dev":
        relational = SQLiteRelationalStoreProvider(tmp_path / "db")
        vectors = InMemoryVectorStoreProvider()
    else:
        from agentruntime_core.adapters.postgres import PostgresRelationalStoreProvider
        from agentruntime_core.adapters.qdrant import QdrantVectorStoreProvider

        cluster: pgtest.Cluster = request.getfixturevalue("pg_cluster")
        qdrant: qdranttest.Instance = request.getfixturevalue("qdrant_instance")
        db = cluster.new_database("memory")
        relational = await PostgresRelationalStoreProvider.connect(cluster.dsn(pgtest.APP_ROLE, db))
        vectors = QdrantVectorStoreProvider(url=qdrant.url, keys=RelationalKeyRing(relational, secrets), prefix=f"mem_{uuid.uuid4().hex[:8]}")
        closers.append(lambda: cluster.drop_database(db))  # noqa: B023 — 只用一次
    keyring = RelationalKeyRing(relational, secrets)
    service = MemoryService(relational=relational, vectors=vectors, keyring=keyring, embedder=HashingEmbedder(), clock=clock)
    await service.setup()
    try:
        yield Backend(name=name, service=service, relational=relational, vectors=vectors, clock=clock, db=db)
    finally:
        await vectors.aclose()
        await relational.aclose()
        for close in closers:
            close()


async def write(service: MemoryService, identity: Identity, *items: dict[str, Any]):
    """提交并处理掉，返回处理报告。"""
    accepted = await service.submit(identity, [(i, TASK_ID) for i in items])
    assert accepted == len(items), f"只接受了 {accepted}/{len(items)} 条"
    return await service.drain(identity)
