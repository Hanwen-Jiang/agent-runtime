"""docs/plan/memory-live.md M-03 验收：Worker 的客户端对真的 Memory Service（gRPC，开发后端）。

Worker 不依赖 agentruntime-memory 包，客户端是自己写的，所以令牌与请求的格式要在这里跨包核对。
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import grpc
import pytest
from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import payload_pb2, tenant_pb2
from agentruntime.memory.v1 import memory_pb2
from agentruntime_core.adapters.keyring import RelationalKeyRing
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.dev.vector_store import InMemoryVectorStoreProvider
from agentruntime_memory.embedding import HashingEmbedder
from agentruntime_memory.identity import DevTokenAuthenticator, Identity
from agentruntime_memory.server import start_server
from agentruntime_memory.service import MemoryService
from agentruntime_worker.memory import WorkerMemoryClient

KEY = b"worker-memory-contract-key-000000"


def tenant(tenant_id: str, user: str) -> tenant_pb2.TenantContext:
    return tenant_pb2.TenantContext(tenant_id=tenant_id, user_id=user)


def candidate(tenant_id: str, user: str, title: str) -> memory_pb2.MemoryCandidate:
    value = struct_pb2.Value()
    json_format.ParseDict(
        {
            "id": "mem_0" + "A" * 25, "kind": "failure",
            "scope": {"tenant": tenant_id, "workspace": None, "user": user, "session": None, "task": None},
            "applies_when": {"task_family": ["engineering.ci_fix"], "tools": [], "model_family": "any"},
            "content": {"title": title, "guidance": "先复现再改"}, "provenance": {"source": "agent_reflection"}, "trust": 0.6,
            "validity": {"valid_from": "2026-10-01", "valid_to": None, "superseded_by": None}, "sensitivity": "none",
            "embedding_version": "pending", "version": 1,
        },
        value,
    )
    return memory_pb2.MemoryCandidate(memory_item=payload_pb2.JsonPayload(value=value), source_task_id="tsk_contract")


@contextlib.asynccontextmanager
async def running(root: Path):
    relational = SQLiteRelationalStoreProvider(root / "db")
    keyring = RelationalKeyRing(relational, LocalFileSecretStoreProvider(root / "secrets.json"))
    svc = MemoryService(relational=relational, vectors=InMemoryVectorStoreProvider(), keyring=keyring, embedder=HashingEmbedder())
    await svc.setup()
    server, port = await start_server(svc, DevTokenAuthenticator(KEY), "127.0.0.1:0", drain_in_background=False)
    try:
        yield svc, f"127.0.0.1:{port}"
    finally:
        await server.stop(0)


def test_the_token_is_accepted_and_a_tenant_sees_only_its_own(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (svc, address):
            client = WorkerMemoryClient(address, KEY)
            alice, bob = tenant("t-alice", "u-a"), tenant("t-bob", "u-b")
            accepted = await client.submit_candidates(memory_pb2.SubmitCandidatesRequest(tenant=alice, candidates=[candidate("t-alice", "u-a", "修持续集成失败要先复现")]))
            assert accepted.accepted == 1
            await svc.drain(Identity(tenant_id="t-alice", user_id="u-a"))
            query = "修持续集成失败"
            mine = await client.search(memory_pb2.SearchRequest(tenant=alice, trigger=memory_pb2.SEARCH_TRIGGER_PLANNING, query=query, top_k=5, token_budget=500))
            theirs = await client.search(memory_pb2.SearchRequest(tenant=bob, trigger=memory_pb2.SEARCH_TRIGGER_PLANNING, query=query, top_k=5, token_budget=500))
            assert len(mine.items) == 1
            assert list(theirs.items) == [], "别的租户查不到"

    asyncio.run(scenario())


def test_a_wrong_signing_key_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (_, address):
            client = WorkerMemoryClient(address, b"another-key-00000000000000000000")
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                await client.search(memory_pb2.SearchRequest(tenant=tenant("t-alice", "u-a"), trigger=memory_pb2.SEARCH_TRIGGER_PLANNING, query="x", top_k=1))
            assert caught.value.code() == grpc.StatusCode.UNAUTHENTICATED

    asyncio.run(scenario())
