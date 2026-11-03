"""给记忆检索评测（Ragas，docs/plan/feedback-2026-11-01.md N-13）用的检索器：真的 MemoryService 代码。

    python -m agentruntime_learning.evals ragas --retriever agentruntime_memory.eval_retriever:dev_retriever --out <目录> …

- 后端是开发用的（临时目录里的 SQLite、内存 VectorStore、开发 SecretStore），评测完删掉；不碰线上的记忆库。
- 嵌入按 wiring.embedder：设了 AR_MODEL_GATEWAY_ADDRESS 与 AR_EMBEDDING_VERSION 时经 Model Gateway 的真嵌入（量检索质量要这样跑），
  否则是哈希嵌入（只检查链路）。
- 评测集的条目走 submit → drain 的写入流水线（同线上：安全检查、去重、冲突），放在一个评测租户的工作区作用域里；被流水线拒掉的
  条目检索不到，分数会如实变低（drain 的报告记在 report 里）。
- 检索用 planning 触发（Planner 规划前取记忆的那条路），按问题取 top_k，结果映射回评测集的条目 id。

live_retriever（docs/plan/memory-live.md 第 5 节，F2）对着已部署的 Memory Service 跑同样的评测，测的是部署：
- 地址 AR_MEMORY_ADDRESS；令牌用 AR_DEV_SIGNING_KEY 现签（与服务端同一把；密钥只留在签令牌的闭包里，不进 repr 与错误信息）；
- 评测租户 evals 下每个检索器一个新工作区，写入走 SubmitCandidates，等后台处理到最后一条再查；aclose 时把写入的逐条删掉。
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import grpc
from agentruntime_core.adapters.keyring import RelationalKeyRing
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.dev.vector_store import InMemoryVectorStoreProvider
from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import payload_pb2, tenant_pb2
from agentruntime.memory.v1 import memory_pb2

from agentruntime_memory.client import MemoryServiceClient
from agentruntime_memory.identity import Identity, issue_dev_token
from agentruntime_memory.service import KINDS, MAX_TOP_K, MemoryService, SearchQuery
from agentruntime_memory.wiring import embedder as make_embedder

EVAL_TENANT = "t-evals"
IDENTITY = Identity(tenant_id=EVAL_TENANT, user_id="u-evals", workspace_id="w-memory-rag")
TASK_ID = "tsk_01J" + "0" * 22 + "E"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def memory_id(n: int) -> str:
    tail = ""
    for _ in range(25):
        tail = _CROCKFORD[n % 32] + tail
        n //= 32
    return "mem_0" + tail


def memory_item(n: int, entry: Mapping[str, str], identity: Identity = IDENTITY, *, item_id: str | None = None) -> dict[str, Any]:
    """评测集的一条（id、kind、text）写成合乎 memory_item.schema.json 的条目（用户在工作区里的作用域：Agent 能直接写的最宽一层；
    agent_reflection、已验证两次）。"""
    kind = entry.get("kind") or "fact"
    if kind not in KINDS:
        raise ValueError(f"{entry.get('id')}：kind 要是 {'、'.join(KINDS)} 之一，得到 {kind!r}")
    text = str(entry["text"])
    return {
        "id": item_id or memory_id(n),
        "scope": {"tenant": identity.tenant_id, "workspace": identity.workspace_id, "user": identity.user_id, "session": None, "task": None},
        "kind": kind,
        "applies_when": {"task_family": ["engineering.general"], "tools": [], "model_family": "any"},
        "content": {"title": text[:80], "guidance": text},
        "provenance": {"source": "agent_reflection"},
        "trust": 0.6,
        "evidence": [{"trace": f"tr_{n:032x}", "outcome": "verified"}, {"trace": f"tr_{n + 10_000:032x}", "outcome": "verified"}],
        "validity": {"valid_from": "2026-01-01", "valid_to": None, "superseded_by": None},
        "sensitivity": "none",
        "embedding_version": "pending",
        "version": 1,
    }


class MemoryEvalRetriever:
    name = "memory-service"

    def __init__(self, corpus: Sequence[Mapping[str, str]], *, env: Mapping[str, str] | None = None) -> None:
        self.corpus = [dict(c) for c in corpus]
        self.env = dict(os.environ if env is None else env)
        self.report: Any = None
        self._dir: str | None = None
        self._service: MemoryService | None = None
        self._stores: list[Any] = []
        self._ids: dict[str, str] = {}

    async def _ready(self) -> MemoryService:
        if self._service is not None:
            return self._service
        self._dir = tempfile.mkdtemp(prefix="memory-rag-")
        root = Path(self._dir)
        relational = SQLiteRelationalStoreProvider(root / "db")
        vectors = InMemoryVectorStoreProvider()
        self._stores = [vectors, relational]
        keyring = RelationalKeyRing(relational, LocalFileSecretStoreProvider(root / "secrets.json"))
        service = MemoryService(relational=relational, vectors=vectors, keyring=keyring, embedder=make_embedder({k: v for k, v in self.env.items() if k != "AR_RELATIONAL_DSN"}))
        await service.setup()
        items = [memory_item(n, entry) for n, entry in enumerate(self.corpus, 1)]
        self._ids = {item["id"]: entry["id"] for item, entry in zip(items, self.corpus, strict=True)}
        await service.submit(IDENTITY, [(item, TASK_ID) for item in items])
        self.report = await service.drain(IDENTITY)
        self._service = service
        return service

    async def retrieve(self, question: str, top_k: int) -> list[dict[str, str]]:
        service = await self._ready()
        found = await service.search(IDENTITY, SearchQuery(trigger="planning", query=question, top_k=top_k))
        texts = {c["id"]: c["text"] for c in self.corpus}
        out = []
        for r in found:
            cid = self._ids.get(str(r.item.get("id")))
            if cid is not None:
                out.append({"id": cid, "text": texts[cid]})
        return out[:top_k]

    async def aclose(self) -> None:
        for store in self._stores:
            await store.aclose()
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)


def dev_retriever(corpus: Sequence[Mapping[str, str]]) -> MemoryEvalRetriever:
    return MemoryEvalRetriever(corpus)


LIVE_TENANT = "evals"
SCHEMA_ID = "https://agentruntime.example/schemas/memory_item.schema.json"
_SUBMIT_BATCH = 50


def _random_memory_id() -> str:
    return "mem_0" + "".join(secrets.choice(_CROCKFORD) for _ in range(25))


def _payload(item: Mapping[str, Any]) -> payload_pb2.JsonPayload:
    value = struct_pb2.Value()
    json_format.ParseDict(dict(item), value)
    return payload_pb2.JsonPayload(schema_id=SCHEMA_ID, value=value)


class LiveMemoryRetriever:
    """对着已部署的 Memory Service：每个检索器一个新工作区，写完等后台处理，查完（aclose）逐条删掉。"""

    name = "memory-service-live"

    def __init__(self, corpus: Sequence[Mapping[str, str]], *, env: Mapping[str, str] | None = None, tenant: str = LIVE_TENANT,
                 user: str = "u-evals", wait_s: float = 300.0) -> None:
        env = dict(os.environ if env is None else env)
        address, key = env.get("AR_MEMORY_ADDRESS", ""), env.get("AR_DEV_SIGNING_KEY", "")
        missing = [n for n, v in (("AR_MEMORY_ADDRESS", address), ("AR_DEV_SIGNING_KEY", key)) if not v]
        if missing:
            raise ValueError(f"对着已部署的 Memory Service 评测要设 {'、'.join(missing)}")
        signing = key.encode()

        def token(ctx: tenant_pb2.TenantContext) -> str:
            return issue_dev_token(signing, tenant=ctx.tenant_id, user=ctx.user_id, workspace=ctx.workspace_id, session=ctx.session_id)

        self.corpus = [dict(c) for c in corpus]
        self.identity = Identity(tenant_id=tenant, user_id=user, workspace_id=f"w-eval-{uuid.uuid4().hex[:16]}")
        self.wait_s = wait_s
        self.accepted = 0
        self.timed_out = False
        self._client = MemoryServiceClient(address, token)
        self._ids: dict[str, str] = {}
        self._written = False

    def __repr__(self) -> str:
        return f"LiveMemoryRetriever(address={self._client.address!r}, workspace={self.identity.workspace_id!r})"

    async def _write(self) -> None:
        if self._written:
            return
        self._written = True
        items = [memory_item(n, entry, self.identity, item_id=_random_memory_id()) for n, entry in enumerate(self.corpus, 1)]
        self._ids = {item["id"]: entry["id"] for item, entry in zip(items, self.corpus, strict=True)}
        ctx = self.identity.tenant_context()
        for start in range(0, len(items), _SUBMIT_BATCH):
            batch = items[start:start + _SUBMIT_BATCH]
            response = await self._client.submit_candidates(memory_pb2.SubmitCandidatesRequest(
                tenant=ctx, candidates=[memory_pb2.MemoryCandidate(memory_item=_payload(item), source_task_id=TASK_ID) for item in batch]))
            self.accepted += response.accepted
        if not items:
            return
        # 后台按顺序处理：最后一条查得到了，前面的也处理完了（被流水线拒掉的查不到，分数如实变低）。等不到就照常查，记下超时。
        last, query = items[-1]["id"], str(items[-1]["content"]["guidance"])
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.wait_s
        while loop.time() < deadline:
            if last in await self._search_ids(query, MAX_TOP_K):
                return
            await asyncio.sleep(0.2)
        self.timed_out = True

    async def _search_ids(self, query: str, top_k: int) -> list[str]:
        found = await self._client.search(memory_pb2.SearchRequest(
            tenant=self.identity.tenant_context(), trigger=memory_pb2.SEARCH_TRIGGER_PLANNING, query=query, top_k=min(top_k, MAX_TOP_K)))
        return [str(json_format.MessageToDict(i.memory_item.value).get("id")) for i in found.items]

    async def retrieve(self, question: str, top_k: int) -> list[dict[str, str]]:
        await self._write()
        texts = {c["id"]: c["text"] for c in self.corpus}
        out = []
        for mid in await self._search_ids(question, top_k):
            cid = self._ids.get(mid)
            if cid is not None:
                out.append({"id": cid, "text": texts[cid]})
        return out[:top_k]

    async def aclose(self) -> None:
        ctx = self.identity.tenant_context()
        try:
            for mid in self._ids:
                try:
                    await self._client.delete(memory_pb2.DeleteRequest(tenant=ctx, memory_id=mid, reason="评测结束"))
                except grpc.aio.AioRpcError:
                    pass  # 被流水线拒掉的条目没有写进去
        finally:
            await self._client.aclose()


def live_retriever(corpus: Sequence[Mapping[str, str]], *, env: Mapping[str, str] | None = None) -> LiveMemoryRetriever:
    return LiveMemoryRetriever(corpus, env=env)


__all__ = ["LiveMemoryRetriever", "MemoryEvalRetriever", "dev_retriever", "live_retriever", "memory_item"]
