"""MemoryService 的 gRPC 服务（proto agentruntime.memory.v1）。

每个调用先认证：metadata 的 authorization: Bearer <令牌> → 身份；请求的 tenant 只用来与身份核对一致。
错误映射：unauthenticated → UNAUTHENTICATED；denied → PERMISSION_DENIED；invalid → INVALID_ARGUMENT；
not_found → NOT_FOUND；其余 → UNAVAILABLE。错误信息不带条目内容。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import grpc
from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import payload_pb2
from agentruntime.memory.v1 import memory_pb2, memory_pb2_grpc
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError, ProviderError

from agentruntime_memory.identity import Authenticator, Identity, bind_request
from agentruntime_memory.service import SCHEMA_ID, Feedback, MemoryService, SearchQuery

T = TypeVar("T")
log = logging.getLogger(__name__)

_TRIGGERS = {
    memory_pb2.SEARCH_TRIGGER_PLANNING: "planning",
    memory_pb2.SEARCH_TRIGGER_NODE_START: "node_start",
    memory_pb2.SEARCH_TRIGGER_ERROR: "error",
}


def _kind(value: int) -> str:
    name = memory_pb2.MemoryKind.Name(value)
    if value == memory_pb2.MEMORY_KIND_UNSPECIFIED:
        raise InvalidError("invalid_kind", "kinds 里有未指定的记忆类型")
    return name.removeprefix("MEMORY_KIND_").lower()


def _payload(item: dict[str, Any]) -> payload_pb2.JsonPayload:
    value = struct_pb2.Value()
    json_format.ParseDict(item, value)
    return payload_pb2.JsonPayload(schema_id=SCHEMA_ID, value=value)


def _status(error: ProviderError) -> grpc.StatusCode:
    if isinstance(error, DeniedError):
        return grpc.StatusCode.UNAUTHENTICATED if error.code == "unauthenticated" else grpc.StatusCode.PERMISSION_DENIED
    if isinstance(error, InvalidError):
        return grpc.StatusCode.INVALID_ARGUMENT
    if isinstance(error, NotFoundError):
        return grpc.StatusCode.NOT_FOUND
    return grpc.StatusCode.UNAVAILABLE


class MemoryServicer(memory_pb2_grpc.MemoryServiceServicer):
    """drain_in_background：提交后在后台处理该租户的队列（独立运行时用；测试里显式调用 drain）。"""

    def __init__(self, service: MemoryService, authenticator: Authenticator, *, drain_in_background: bool = False) -> None:
        self.service = service
        self.authenticator = authenticator
        self.drain_in_background = drain_in_background
        self._drains: set[asyncio.Task[None]] = set()

    async def _drain(self, identity: Identity) -> None:
        try:
            await self.service.drain(identity)
        except Exception as error:  # noqa: BLE001 — 留在队列里，下次提交时再处理
            log.warning("后台处理写入队列失败（租户 %s）：%s", identity.tenant_id, type(error).__name__)

    def _identity(self, request: Any, context: grpc.aio.ServicerContext) -> Identity:
        metadata = dict(context.invocation_metadata() or ())
        scheme, _, token = str(metadata.get("authorization", "")).partition(" ")
        if scheme.lower() != "bearer":
            raise DeniedError("unauthenticated", "缺少 Bearer 令牌")
        identity = self.authenticator.authenticate(token.strip())
        return bind_request(identity, request.tenant if request.HasField("tenant") else None)

    async def _call(self, request: Any, context: grpc.aio.ServicerContext, handler: Callable[[Identity], Awaitable[T]]) -> T:
        try:
            return await handler(self._identity(request, context))
        except ProviderError as error:
            await context.abort(_status(error), f"{error.code}: {error.message}")
        except Exception:  # noqa: BLE001 — 不把内部错误的细节（可能含条目内容）带给调用方
            await context.abort(grpc.StatusCode.UNAVAILABLE, "internal: Memory Service 内部错误")
        raise AssertionError("unreachable")

    async def Search(self, request: memory_pb2.SearchRequest, context: grpc.aio.ServicerContext) -> memory_pb2.SearchResponse:
        async def handle(identity: Identity) -> memory_pb2.SearchResponse:
            trigger = _TRIGGERS.get(request.trigger)
            if trigger is None:
                raise InvalidError("invalid_trigger", "trigger 未指定")
            query = SearchQuery(
                trigger=trigger,
                query=request.query,
                task_family=request.task_family,
                tools=tuple(request.tools),
                error_signature=request.error_signature,
                kinds=tuple(_kind(k) for k in request.kinds),
                token_budget=request.token_budget,
                top_k=request.top_k,
            )
            hits = await self.service.search(identity, query)
            return memory_pb2.SearchResponse(items=[memory_pb2.RetrievedItem(memory_item=_payload(h.item), score=h.score) for h in hits])

        return await self._call(request, context, handle)

    async def SubmitCandidates(self, request: memory_pb2.SubmitCandidatesRequest, context: grpc.aio.ServicerContext) -> memory_pb2.SubmitCandidatesResponse:
        async def handle(identity: Identity) -> memory_pb2.SubmitCandidatesResponse:
            candidates = [(json_format.MessageToDict(c.memory_item.value), c.source_task_id) for c in request.candidates]
            accepted = await self.service.submit(identity, candidates)
            if accepted and self.drain_in_background:
                task = asyncio.create_task(self._drain(identity))
                self._drains.add(task)
                task.add_done_callback(self._drains.discard)
            return memory_pb2.SubmitCandidatesResponse(accepted=accepted)

        return await self._call(request, context, handle)

    async def RecordFeedback(self, request: memory_pb2.RecordFeedbackRequest, context: grpc.aio.ServicerContext) -> memory_pb2.RecordFeedbackResponse:
        async def handle(identity: Identity) -> memory_pb2.RecordFeedbackResponse:
            feedback = [Feedback(memory_id=f.memory_id, adopted=f.adopted, task_succeeded=f.task_succeeded) for f in request.feedback]
            await self.service.feedback(identity, request.task_id, feedback)
            return memory_pb2.RecordFeedbackResponse()

        return await self._call(request, context, handle)

    async def Delete(self, request: memory_pb2.DeleteRequest, context: grpc.aio.ServicerContext) -> memory_pb2.DeleteResponse:
        async def handle(identity: Identity) -> memory_pb2.DeleteResponse:
            deleted = await self.service.delete(identity, request.memory_id, request.reason)
            return memory_pb2.DeleteResponse(deleted_memory_ids=deleted)

        return await self._call(request, context, handle)


async def start_server(
    service: MemoryService, authenticator: Authenticator, address: str = "127.0.0.1:0", *, drain_in_background: bool = False,
    knowledge: Any = None, knowledge_internal_token: str = ""
) -> tuple[grpc.aio.Server, int]:
    """启动 gRPC 服务，返回（服务, 端口）。"""
    server = grpc.aio.server(options=(("grpc.max_receive_message_length",24<<20),("grpc.max_send_message_length",16<<20)))
    memory_pb2_grpc.add_MemoryServiceServicer_to_server(MemoryServicer(service, authenticator, drain_in_background=drain_in_background), server)
    if knowledge is not None:
        from agentruntime_memory.knowledge import register_knowledge
        register_knowledge(server, knowledge, authenticator, knowledge_internal_token)
    port = server.add_insecure_port(address)
    await server.start()
    return server, port
