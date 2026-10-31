"""Worker 接 Memory Service（docs/plan/memory-live.md M-03）。

- 没设 AR_MEMORY_ADDRESS 时行为不变：候选只记在进程内，不检索。
- Worker 不依赖 agentruntime-memory 包（它会带上向量库的依赖），这里只用 proto 的存根；令牌格式与它的 issue_dev_token 相同，
  由 tests/contract/test_worker_memory.py 核对。令牌按 Task 的租户现签。
- 检索失败不拖垮 Task：当作没有经验，日志只记异常类型（异常的文字可能带任务内容）。
- 检索到的经验是数据，不是指令；全部在 activity 里调用，不改工作流的命令序列。
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import hmac
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any, Protocol

import grpc
from google.protobuf import json_format

from agentruntime.memory.v1 import memory_pb2, memory_pb2_grpc
from agentruntime_worker.activities.types import PlanRoundInput, Tenant
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.context.builder import tenant_context
from agentruntime_worker.planner.context import PlanningContext, PlanningContextSource
from agentruntime_worker.reviewer.wrap_up import MemoryClient, RecordingMemoryClient

log = logging.getLogger(__name__)

TOKEN_TTL_S = 300
SEARCH_TIMEOUT_S = 5.0
SUBMIT_TIMEOUT_S = 30.0
PLANNING_TOP_K = 5
NODE_TOP_K = 3
TOKEN_BUDGET = 600
# tool_loop 的第一步（Stage Runner 的 iteration 从 1 数）。
FIRST_ITERATION = 1
# 一个 Node 第一步查到的经验，之后的步骤接着用（否则第二步起模型就看不到了）。在别的 Worker 上继续时没有，当作没有经验。
NODE_CACHE_SIZE = 256
_node_cache: OrderedDict[tuple[str, str, str], tuple[str, ...]] = OrderedDict()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Searcher(Protocol):
    async def search(self, request: memory_pb2.SearchRequest) -> memory_pb2.SearchResponse: ...


class WorkerMemoryClient:
    def __init__(self, address: str, signing_key: bytes) -> None:
        self.address = address
        self._key = signing_key
        self._stub: memory_pb2_grpc.MemoryServiceStub | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _client(self) -> memory_pb2_grpc.MemoryServiceStub:
        loop = asyncio.get_running_loop()
        if self._stub is None or self._loop is not loop:
            self._stub, self._loop = memory_pb2_grpc.MemoryServiceStub(grpc.aio.insecure_channel(self.address)), loop
        return self._stub

    def metadata(self, tenant_id: str, user_id: str) -> tuple[tuple[str, str], ...]:
        claims = {"t": tenant_id, "u": user_id or "worker", "exp": int(time.time()) + TOKEN_TTL_S}
        payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
        token = payload + "." + _b64(hmac.new(self._key, payload.encode(), hashlib.sha256).digest())
        return (("authorization", f"Bearer {token}"),)

    async def search(self, request: memory_pb2.SearchRequest) -> memory_pb2.SearchResponse:
        return await self._client().Search(request, metadata=self.metadata(request.tenant.tenant_id, request.tenant.user_id), timeout=SEARCH_TIMEOUT_S)

    async def submit_candidates(self, request: memory_pb2.SubmitCandidatesRequest) -> memory_pb2.SubmitCandidatesResponse:
        return await self._client().SubmitCandidates(request, metadata=self.metadata(request.tenant.tenant_id, request.tenant.user_id), timeout=SUBMIT_TIMEOUT_S)


def memory_client(config: WorkerConfig) -> MemoryClient:
    """设了 AR_MEMORY_ADDRESS 时是真客户端，否则是开发用的记录器。"""
    if not config.memory_address:
        return RecordingMemoryClient()
    if not config.dev_signing_key:
        raise ValueError("设了 AR_MEMORY_ADDRESS 就要设 AR_DEV_SIGNING_KEY：Memory Service 的令牌要它签")
    return WorkerMemoryClient(config.memory_address, config.dev_signing_key.encode())


def searcher(config: WorkerConfig) -> Searcher | None:
    client = memory_client(config)
    return client if isinstance(client, WorkerMemoryClient) else None


def _render(item: Mapping[str, Any]) -> str:
    content = item.get("content") or {}
    source = (item.get("provenance") or {}).get("source", "unknown")
    title, guidance = json.dumps(str(content.get("title", "")), ensure_ascii=False), json.dumps(str(content.get("guidance", "")), ensure_ascii=False)
    return f"[{item.get('kind', '')}] 来源 {source}，信任 {float(item.get('trust', 0.0)):.2f}：标题 {title}，指导 {guidance}"


async def _search(memory: Searcher, request: memory_pb2.SearchRequest) -> tuple[str, ...]:
    try:
        response = await memory.search(request)
        return tuple(_render(json_format.MessageToDict(r.memory_item.value)) for r in response.items)
    except Exception as err:  # noqa: BLE001 — 记忆只是增强，任何失败都不影响 Task
        log.warning("检索经验失败，这次不用经验：%s", type(err).__name__)
        return ()


class RetrievalContextSource:
    """规划时查经验，放进规划上下文。"""

    def __init__(self, inner: PlanningContextSource, memory: Searcher) -> None:
        self.inner = inner
        self.memory = memory

    async def load(self, inp: PlanRoundInput) -> PlanningContext:
        context = await self.inner.load(inp)
        experiences = await _search(self.memory, memory_pb2.SearchRequest(
            tenant=tenant_context(inp.tenant), trigger=memory_pb2.SEARCH_TRIGGER_PLANNING, query=inp.message, token_budget=TOKEN_BUDGET, top_k=PLANNING_TOP_K,
        ))
        return dataclasses.replace(context, experiences=experiences)


async def node_experiences(memory: Searcher | None, tenant: Tenant, task_id: str, node: Mapping[str, Any], *, iteration: int) -> tuple[str, ...]:
    """tool_loop 的一步要带的经验：第一步查，之后用第一步查到的。"""
    if memory is None:
        return ()
    key = (tenant.tenant_id, task_id, str(node.get("id", "")))
    if iteration != FIRST_ITERATION:
        return _node_cache.get(key, ())
    found = await _search(memory, memory_pb2.SearchRequest(
        tenant=tenant_context(tenant), trigger=memory_pb2.SEARCH_TRIGGER_NODE_START, query=str(node.get("goal", "")),
        tools=[str(t) for t in node.get("tools") or []], token_budget=TOKEN_BUDGET, top_k=NODE_TOP_K,
    ))
    _node_cache[key] = found
    while len(_node_cache) > NODE_CACHE_SIZE:
        _node_cache.popitem(last=False)
    return found
