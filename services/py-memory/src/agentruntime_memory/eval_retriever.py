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
- 运行前用 AR_MEMORY_TOPOLOGY_EVIDENCE 核实且冻结单写者拓扑；每题独立 evals 工作区，FIFO 哨兵确认处理后删除哨兵，再检索；aclose 逐条核验清理，失败保留诊断。
"""

from __future__ import annotations

import asyncio
import json
import math
import hashlib
import os
import secrets
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
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


def single_writer_evidence(path: str) -> dict[str, Any]:
    """Validate a pre-run `kubectl get deployment,pods -l ... -o json`
    snapshot. The release coordinator must freeze this topology for the run;
    a process-local FIFO lock does not support multiple Memory replicas.
    """
    if not path:
        raise ValueError("live evaluation requires AR_MEMORY_TOPOLOGY_EVIDENCE")
    raw = Path(path).read_bytes()
    evidence = json.loads(raw)
    items = evidence.get("items", [])
    deployments = [i for i in items if i.get("kind") == "Deployment"]
    pods = [i for i in items if i.get("kind") == "Pod"]
    if len(deployments) != 1 or len(pods) != 1:
        raise ValueError("memory topology must contain exactly one Deployment and one Pod")
    deployment, pod = deployments[0], pods[0]
    status, spec, meta = deployment.get("status", {}), deployment.get("spec", {}), deployment.get("metadata", {})
    selector = spec.get("selector", {}).get("matchLabels", {})
    pod_meta, pod_status = pod.get("metadata", {}), pod.get("status", {})
    if (spec.get("replicas") != 1 or any(status.get(k) != 1 for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"))
            or status.get("observedGeneration", 0) < meta.get("generation", 1) or not selector
            or any(pod_meta.get("labels", {}).get(k) != v for k, v in selector.items())
            or pod_meta.get("deletionTimestamp") or pod_status.get("phase") != "Running"
            or not any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod_status.get("conditions", []))):
        raise ValueError("memory topology is not a stable single writer")
    containers = spec.get("template", {}).get("spec", {}).get("containers", [])
    return {"sha256": hashlib.sha256(raw).hexdigest(), "deployment_uid": meta.get("uid"),
            "pod_uid": pod_meta.get("uid"), "generation": meta.get("generation"),
            "images": [c.get("image", "") for c in containers]}


class LiveMemoryRetriever:
    """对着已部署的 Memory Service：每个检索器一个新工作区，写完等后台处理，查完（aclose）逐条删掉。"""

    name = "memory-service-live"

    def __init__(self, corpus: Sequence[Mapping[str, str]], *, env: Mapping[str, str] | None = None, tenant: str = LIVE_TENANT,
                 user: str = "u-evals", wait_s: float = 300.0, rpc_timeout_s: float = 30.0, single_writer_verified: bool = False) -> None:
        env = dict(os.environ if env is None else env)
        address, key = env.get("AR_MEMORY_ADDRESS", ""), env.get("AR_DEV_SIGNING_KEY", "")
        missing = [n for n, v in (("AR_MEMORY_ADDRESS", address), ("AR_DEV_SIGNING_KEY", key)) if not v]
        if missing:
            raise ValueError(f"对着已部署的 Memory Service 评测要设 {'、'.join(missing)}")
        signing = key.encode()

        def token(ctx: tenant_pb2.TenantContext) -> str:
            return issue_dev_token(signing, tenant=ctx.tenant_id, user=ctx.user_id, workspace=ctx.workspace_id, session=ctx.session_id)

        topology = {} if single_writer_verified else single_writer_evidence(env.get("AR_MEMORY_TOPOLOGY_EVIDENCE", ""))
        self.corpus = [dict(c) for c in corpus]
        self.identity = Identity(tenant_id=tenant, user_id=user, workspace_id=f"w-eval-{uuid.uuid4().hex[:16]}")
        if not math.isfinite(wait_s) or not math.isfinite(rpc_timeout_s) or wait_s <= 0 or rpc_timeout_s <= 0:
            raise ValueError("wait_s and rpc_timeout_s must be positive")
        self.wait_s = wait_s
        self.rpc_timeout_s = rpc_timeout_s
        self.accepted = 0
        self.timed_out = False
        self._client = MemoryServiceClient(address, token)
        self._ids: dict[str, str] = {}
        self._written = False
        self._submitted: list[str] = []
        self._barrier: tuple[str, str] | None = None
        self._drained = False
        self._closed = False
        self._lifecycle_sink: Callable[[dict[str, Any]], None] | None = None
        self.diagnostics: dict[str, Any] = {
            "workspace": self.identity.workspace_id, "tenant": tenant, "user": user, "topology": topology,
            "attempted": 0, "accepted": 0, "timed_out": False, "pipeline_complete": False,
            "deleted": [], "not_found": [], "cleanup_errors": {}, "cleanup_complete": False,
        }

    def __repr__(self) -> str:
        return f"LiveMemoryRetriever(address={self._client.address!r}, workspace={self.identity.workspace_id!r})"

    def set_lifecycle_sink(self, sink: Callable[[dict[str, Any]], None]) -> None:
        """Persist workspace/IDs before admitting writes, including a crash mid-batch."""
        self._lifecycle_sink = sink
        self._checkpoint()

    def _checkpoint(self) -> None:
        if self._lifecycle_sink is not None:
            self._lifecycle_sink(self.diagnostics)

    async def _rpc(self, request: Any) -> Any:
        # The underlying client has no deadlines; an unreachable service must not
        # hang an evaluation or its cleanup indefinitely.
        return await asyncio.wait_for(request, timeout=self.rpc_timeout_s)

    async def _submit(self, items: list[dict[str, Any]]) -> None:
        # Track before the call: a timeout can occur after the server committed.
        self._submitted.extend(item["id"] for item in items)
        self.diagnostics["submitted_ids"] = list(self._submitted)
        self.diagnostics["attempted"] += len(items)
        self._checkpoint()
        response = await self._rpc(self._client.submit_candidates(memory_pb2.SubmitCandidatesRequest(
            tenant=self.identity.tenant_context(),
            candidates=[memory_pb2.MemoryCandidate(memory_item=_payload(item), source_task_id=TASK_ID) for item in items])))
        self.accepted += response.accepted
        self.diagnostics["accepted"] = self.accepted
        self._checkpoint()
        if response.accepted != len(items):
            raise RuntimeError("memory_candidate_not_accepted")

    async def _wait_for_pipeline(self) -> None:
        if self._drained or not self._barrier:
            return
        mid, query = self._barrier
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.wait_s
        delay = 0.5
        while loop.time() < deadline:
            try:
                remaining = deadline - loop.time()
                found = await asyncio.wait_for(self._search_ids(query, MAX_TOP_K), timeout=remaining)
                if mid in found:
                    self._drained = True
                    self.diagnostics["pipeline_complete"] = True
                    self._checkpoint()
                    return
            except (TimeoutError, grpc.aio.AioRpcError):
                pass
            await asyncio.sleep(min(delay, max(0, deadline - loop.time())))
            delay = min(delay * 2, 5.0)
        self.timed_out = True
        self.diagnostics["timed_out"] = True
        self._checkpoint()
        raise TimeoutError("memory_pipeline_timeout")

    async def _write(self) -> None:
        if self._written:
            return
        self._written = True
        items = [memory_item(n, entry, self.identity, item_id=_random_memory_id()) for n, entry in enumerate(self.corpus, 1)]
        self._ids = {item["id"]: entry["id"] for item, entry in zip(items, self.corpus, strict=True)}
        # Corpus entries may be screened out or merged. A separate neutral,
        # unique sentinel establishes FIFO completion without relying on the
        # last dataset entry surviving screening. It is deleted before scoring.
        marker = f"The evaluation batch reference is {uuid.uuid4().hex}."
        sentinel = memory_item(len(items) + 1, {"id": "sentinel", "kind": "fact", "text": marker}, self.identity,
                               item_id=_random_memory_id())
        self._barrier = (sentinel["id"], marker)
        for start in range(0, len(items), _SUBMIT_BATCH):
            await self._submit(items[start:start + _SUBMIT_BATCH])
        await self._submit([sentinel])
        await self._wait_for_pipeline()
        await self._delete(sentinel["id"])

    async def _search_ids(self, query: str, top_k: int) -> list[str]:
        found = await self._rpc(self._client.search(memory_pb2.SearchRequest(
            tenant=self.identity.tenant_context(), trigger=memory_pb2.SEARCH_TRIGGER_PLANNING, query=query, top_k=min(top_k, MAX_TOP_K))))
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

    async def _delete(self, mid: str) -> None:
        if mid in self.diagnostics["deleted"] or mid in self.diagnostics["not_found"]:
            return
        try:
            response = await self._rpc(self._client.delete(memory_pb2.DeleteRequest(
                tenant=self.identity.tenant_context(), memory_id=mid, reason="评测结束")))
            if mid not in response.deleted_memory_ids:
                raise RuntimeError("memory_delete_unconfirmed")
            self.diagnostics["deleted"].append(mid)
        except grpc.aio.AioRpcError as exc:
            # Only after FIFO completion does NOT_FOUND mean rejected/merged,
            # rather than a queued item that could appear after cleanup.
            if exc.code() != grpc.StatusCode.NOT_FOUND or not self._drained:
                raise
            self.diagnostics["not_found"].append(mid)
        self._checkpoint()

    async def aclose(self) -> None:
        if self._closed:
            return
        failures: dict[str, str] = self.diagnostics["cleanup_errors"]
        try:
            if self._submitted and not self._drained:
                try:
                    # Submit may have failed before the barrier was queued. A
                    # fresh sentinel also wakes a previously failed drain.
                    if self._barrier:
                        old, marker = self._barrier
                        if old not in self._submitted:
                            sentinel = memory_item(len(self.corpus) + 1, {"id": "sentinel", "kind": "fact", "text": marker},
                                                   self.identity, item_id=old)
                            await self._submit([sentinel])
                    await self._wait_for_pipeline()
                except Exception as exc:
                    failures["pipeline"] = type(exc).__name__
            for mid in self._submitted:
                for attempt in range(3):
                    try:
                        await self._delete(mid)
                        failures.pop(mid, None)
                        break
                    except Exception as exc:
                        code = exc.code().name if isinstance(exc, grpc.aio.AioRpcError) else type(exc).__name__
                        failures[mid] = code
                        if attempt < 2:
                            await asyncio.sleep(0.1 * (attempt + 1))
            self.diagnostics["pending_ids"] = [mid for mid in self._submitted
                                                if mid not in self.diagnostics["deleted"] and mid not in self.diagnostics["not_found"]]
            self.diagnostics["cleanup_complete"] = not failures and (not self._submitted or self._drained)
            self._checkpoint()
        finally:
            self._closed = True
            await self._client.aclose()
        if not self.diagnostics["cleanup_complete"]:
            raise RuntimeError("memory_cleanup_incomplete")


def live_retriever(corpus: Sequence[Mapping[str, str]], *, env: Mapping[str, str] | None = None) -> LiveMemoryRetriever:
    settings = os.environ if env is None else env
    return LiveMemoryRetriever(corpus, env=settings, wait_s=float(settings.get("AR_MEMORY_EVAL_WAIT_S", "300")))


__all__ = ["LiveMemoryRetriever", "MemoryEvalRetriever", "dev_retriever", "live_retriever", "memory_item"]
