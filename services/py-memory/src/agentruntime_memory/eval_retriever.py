"""给记忆检索评测（Ragas，docs/plan/feedback-2026-11-01.md N-13）用的检索器：真的 MemoryService 代码。

    python -m agentruntime_learning.evals ragas --retriever agentruntime_memory.eval_retriever:dev_retriever --out <目录> …

- 后端是开发用的（临时目录里的 SQLite、内存 VectorStore、开发 SecretStore），评测完删掉；不碰线上的记忆库。
- 嵌入按 wiring.embedder：设了 AR_MODEL_GATEWAY_ADDRESS 与 AR_EMBEDDING_VERSION 时经 Model Gateway 的真嵌入（量检索质量要这样跑），
  否则是哈希嵌入（只检查链路）。
- 评测集的条目走 submit → drain 的写入流水线（同线上：安全检查、去重、冲突），放在一个评测租户的工作区作用域里；被流水线拒掉的
  条目检索不到，分数会如实变低（drain 的报告记在 report 里）。
- 检索用 planning 触发（Planner 规划前取记忆的那条路），按问题取 top_k，结果映射回评测集的条目 id。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agentruntime_core.adapters.keyring import RelationalKeyRing
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.dev.vector_store import InMemoryVectorStoreProvider

from agentruntime_memory.identity import Identity
from agentruntime_memory.service import KINDS, MemoryService, SearchQuery
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


def memory_item(n: int, entry: Mapping[str, str]) -> dict[str, Any]:
    """评测集的一条（id、kind、text）写成合乎 memory_item.schema.json 的条目（用户在工作区里的作用域：Agent 能直接写的最宽一层；
    agent_reflection、已验证两次）。"""
    kind = entry.get("kind") or "fact"
    if kind not in KINDS:
        raise ValueError(f"{entry.get('id')}：kind 要是 {'、'.join(KINDS)} 之一，得到 {kind!r}")
    text = str(entry["text"])
    return {
        "id": memory_id(n),
        "scope": {"tenant": EVAL_TENANT, "workspace": IDENTITY.workspace_id, "user": IDENTITY.user_id, "session": None, "task": None},
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


__all__ = ["MemoryEvalRetriever", "dev_retriever", "memory_item"]
