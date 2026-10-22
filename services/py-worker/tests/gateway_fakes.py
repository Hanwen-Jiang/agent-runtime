"""测试用的假 ModelGatewayService：grpc.aio 在进程内起服务，记录收到的请求，按设定返回响应或错误。

错误的形状照 Go 侧 Model Gateway 的约定（docs/plan/model-gateway-direct.md“交付 2”）：
gRPC 状态码 + trailer ar-error-bin（序列化的 agentruntime.common.v1.Error）；transient 带 details.retry_after_ms。
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import grpc
from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import enums_pb2, error_pb2, metering_pb2, payload_pb2, tenant_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime.modelgateway.v1 import model_gateway_pb2_grpc as mg_grpc

from agentruntime_worker.executor.grpc_gateway import GrpcModelGateway

# 约定 3 的 trailer 名与 details 字段名（这里照约定写死，不从被测模块取，用来核对约定）。
ERROR_TRAILER = "ar-error-bin"
RETRY_AFTER_DETAIL = "retry_after_ms"

# 有辨识度的消息内容：断言错误文本里不出现它。
SECRET_MESSAGE = "机密消息-7f3a9c：客户 C-1024 的退款明细"

TENANT = tenant_pb2.TenantContext(
    tenant_id="t-grpc",
    user_id="u-grpc",
    workspace_id="w-grpc",
    session_id="ses-grpc",
    trace=tenant_pb2.TraceContext(traceparent="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01", tracestate="ar=1"),
)


def value(data: Any) -> struct_pb2.Value:
    out = struct_pb2.Value()
    json_format.ParseDict(data, out)
    return out


def struct(data: dict[str, Any]) -> struct_pb2.Struct:
    out = struct_pb2.Struct()
    out.update(data)
    return out


def call_context(**overrides: Any) -> mg.CallContext:
    fields: dict[str, Any] = {
        "task_id": "tsk_grpc",
        "node_id": "answer",
        "role": enums_pb2.ROLE_DELIVER,
        "task_family": "qa",
        "tier": "mid@low",
        "reasoning": enums_pb2.REASONING_STEPWISE,
        "pinned_model_profile": "fake/mid@2026-10",
        "avoid_vendor": "anthropic",
        "max_cost_usd_micros": 250_000,
    }
    fields.update(overrides)
    return mg.CallContext(**fields)


def generate_request(text: str = SECRET_MESSAGE) -> mg.GenerateRequest:
    """每个字段都填上值，往返时逐一比对。"""
    return mg.GenerateRequest(
        tenant=TENANT,
        call=call_context(),
        generation=mg.GenerateParams(
            messages=[
                mg.ChatMessage(role=mg.CHAT_ROLE_SYSTEM, text="你是测试助手。", cache_breakpoint=True),
                mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=text),
                mg.ChatMessage(
                    role=mg.CHAT_ROLE_ASSISTANT,
                    tool_calls=[mg.ToolCall(id="call-1", name="lookup", arguments=struct({"q": "退款"}))],
                ),
                mg.ChatMessage(role=mg.CHAT_ROLE_TOOL, text='{"rows": 3}', tool_call_id="call-1"),
            ],
            tools=[
                mg.ToolSpec(
                    name="lookup",
                    description="查一条记录",
                    params_schema=payload_pb2.JsonPayload(value=value({"type": "object", "properties": {"q": {"type": "string"}}})),
                )
            ],
            output_schema=payload_pb2.JsonPayload(value=value({"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]})),
            max_output_tokens=512,
            thinking_budget_tokens=2048,
        ),
    )


def generate_response() -> mg.GenerateResponse:
    return mg.GenerateResponse(
        text="上季度退款率上升的主要原因是物流延误。",
        result=value({"answer": "物流延误", "confidence": 0.75}),
        tool_calls=[mg.ToolCall(id="call-2", name="lookup", arguments=struct({"q": "物流"}))],
        rationale="先看退款原因的分布，再看时间趋势。",
        finish_reason=mg.FINISH_REASON_STOP,
        model=mg.ModelChoice(model_profile_id="fake/mid@2026-10", tier="mid@low", vendor="openai"),
        usage=metering_pb2.Usage(
            input_tokens=1200, output_tokens=300, cache_read_tokens=200, cache_write_tokens=50, reasoning_tokens=120, cost_usd_micros=4500
        ),
    )


def embed_request() -> mg.EmbedRequest:
    return mg.EmbedRequest(
        tenant=TENANT,
        call=call_context(node_id="embed", role=enums_pb2.ROLE_FETCH, tier="embed@default", reasoning=enums_pb2.REASONING_UNSPECIFIED),
        texts=["第一段文本", "第二段文本"],
        embedding_version="fake/embed@2026-10",
    )


def embed_response() -> mg.EmbedResponse:
    # 取二进制精确可表示的值：float32 往返后仍然相等。
    return mg.EmbedResponse(
        embeddings=[mg.Embedding(values=[0.5, -0.25, 0.125]), mg.Embedding(values=[1.0, 0.0, -1.0])],
        embedding_version="fake/embed@2026-10",
        model=mg.ModelChoice(model_profile_id="fake/embed@2026-10", tier="embed@default", vendor="openai"),
        usage=metering_pb2.Usage(input_tokens=5000, cost_usd_micros=100),
    )


def provider_error(category: int, code: str, message: str, details: dict[str, Any] | None = None) -> error_pb2.Error:
    err = error_pb2.Error(category=category, code=code, message=message)
    if details:
        err.details.update(details)
    return err


@dataclass
class Abort:
    """让假服务以这个状态码失败；error 不为 None 时把它序列化后放进 trailer ar-error-bin。

    raw_trailer 不为 None 时 trailer 原样放这些字节（优先于 error），用来模拟损坏或意外的 trailer。
    """

    code: grpc.StatusCode
    details: str = ""
    error: error_pb2.Error | None = None
    raw_trailer: bytes | None = None

    def trailer(self) -> tuple[tuple[str, bytes], ...]:
        if self.raw_trailer is not None:
            return ((ERROR_TRAILER, self.raw_trailer),)
        if self.error is not None:
            return ((ERROR_TRAILER, self.error.SerializeToString()),)
        return ()


@dataclass
class FakeModelGateway(mg_grpc.ModelGatewayServiceServicer):
    """记录请求（按到达顺序），返回设定的响应；abort 不为 None 时失败；delay_s 大于 0 时先等待（测试客户端超时）。"""

    generate_reply: mg.GenerateResponse = field(default_factory=generate_response)
    embed_reply: mg.EmbedResponse = field(default_factory=embed_response)
    abort: Abort | None = None
    delay_s: float = 0.0
    requests: list[Any] = field(default_factory=list)

    async def Generate(self, request: mg.GenerateRequest, context: grpc.aio.ServicerContext) -> mg.GenerateResponse:  # noqa: N802 — gRPC 方法名
        return await self._answer(request, context, self.generate_reply)

    async def Embed(self, request: mg.EmbedRequest, context: grpc.aio.ServicerContext) -> mg.EmbedResponse:  # noqa: N802 — gRPC 方法名
        return await self._answer(request, context, self.embed_reply)

    async def _answer(self, request: Any, context: grpc.aio.ServicerContext, response: Any) -> Any:
        self.requests.append(request)
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        if self.abort is not None:
            await context.abort(self.abort.code, self.abort.details, self.abort.trailer())
        return response


@asynccontextmanager
async def serve(fake: FakeModelGateway, port: int = 0) -> AsyncIterator[str]:
    """在 127.0.0.1 上起假服务，返回地址 host:port；port 为 0 时随机分配。"""
    server = grpc.aio.server()
    mg_grpc.add_ModelGatewayServiceServicer_to_server(fake, server)
    bound = server.add_insecure_port(f"127.0.0.1:{port}")
    await server.start()
    try:
        yield f"127.0.0.1:{bound}"
    finally:
        await server.stop(None)


@asynccontextmanager
async def open_client(address: str, **kwargs: Any) -> AsyncIterator[GrpcModelGateway]:
    gateway = GrpcModelGateway(address, **kwargs)
    try:
        yield gateway
    finally:
        await gateway.aclose()


def free_port() -> int:
    """先拿一个空闲端口：客户端要在事件循环之外、服务起来之前构造时用。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
