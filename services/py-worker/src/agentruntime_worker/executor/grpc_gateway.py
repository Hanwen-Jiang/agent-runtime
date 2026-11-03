"""Model Gateway 的 gRPC 客户端（设计书 3.3.1、4.8；工作项“Model Gateway 单供应商直连”，见 docs/plan/model-gateway-direct.md）。

GrpcModelGateway 实现 executor.model_gateway.ModelGateway：经 grpcio 调用 go-model-gateway 的 ModelGatewayService
（与 /healthz 同一端口上的 h2c gRPC）。Worker 设了 AR_MODEL_GATEWAY_ADDRESS 时，Node Executor、Planner、Reviewer
的模型调用都用它；没设时仍用桩模型 activities.stubs.StubModelGateway（端到端测试的默认不变）。

错误还原（与 Go 侧的约定）：
- trailer ar-error-bin 是序列化的 agentruntime.common.v1.Error：按类别还原为 ProviderError（ProviderError.from_proto），
  code 与 message 原样保留；transient 时 details 里的 retry_after_ms（整数毫秒）还原为 retry_after，
  它不是非负数时忽略（retry_after 为 None）。details 里的 billed_cost_usd_micros（失败之前已经计费的花费）还原到错误上，
  调用方用 executor.model_gateway.billed_cost_usd 取出（TMP 计划第 11.11 节）。
- 没有这个 trailer、trailer 解析不了、或类别是 UNSPECIFIED 与不认识的值时，按 gRPC 状态码映射：UNAVAILABLE、DEADLINE_EXCEEDED、RESOURCE_EXHAUSTED → TransientError；
  INVALID_ARGUMENT → InvalidError；PERMISSION_DENIED、UNAUTHENTICATED → DeniedError；NOT_FOUND → NotFoundError；
  其余 → TransientError。
- 每次调用的 deadline 是 timeout_s；在 Temporal activity 里调用时，还不超过 activity 剩下的时间减去 ACTIVITY_MARGIN_S
  （gRPC 把 deadline 带给网关，网关据此安排每次尝试的超时、重试与切换，不会等 activity 先超时、再由 Temporal 整体重试，
  让同一个请求在上游被处理两次）。activity 已经没有时间时不发请求，直接抛 TransientError（code activity_deadline）。
  客户端超时是 DEADLINE_EXCEEDED，按上一条为 TransientError。
- 错误信息里不出现请求的消息内容（附录 D.4、CLAUDE.md“凭据与密钥不得出现在日志、trace 和错误信息里”）。
- 不把 grpc 的异常漏给调用方：一律抛 agentruntime_core.errors 的 ProviderError。

from_env() 读 AR_MODEL_GATEWAY_ADDRESS，可直接作为评测的 AR_EVAL_GATEWAY_FACTORY
（agentruntime_worker.executor.grpc_gateway:from_env，见 py-learning 的 evals.worker_subject）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
from google.protobuf.message import DecodeError
from temporalio import activity

from agentruntime.common.v1 import error_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime.modelgateway.v1 import model_gateway_pb2_grpc as mg_grpc
from agentruntime_core.errors import DeniedError, ErrorCategory, InvalidError, NotFoundError, ProviderError, TransientError

from agentruntime_worker.config import ENV_MODEL_GATEWAY_ADDRESS
from agentruntime_worker.executor.model_gateway import with_billed_cost

# trailer 的名字：值是序列化的 agentruntime.common.v1.Error（proto 二进制；-bin 后缀表示二进制元数据）。
ERROR_TRAILER = "ar-error-bin"
# Error.details 里的建议重试间隔（整数毫秒），只在 transient 时出现。
RETRY_AFTER_DETAIL = "retry_after_ms"
# 网关的 models_exhausted 在 details 里带上最后一次失败的代码（TMP 计划 eval-observability-and-runtime-fixes 第 4 节），写进说明里。
LAST_ERROR_CODE_DETAIL = "last_error_code"
# details 里失败之前已经计费的花费（微美元），与 Go 侧 connecterr.BilledCostDetail 同名。
BILLED_COST_DETAIL = "billed_cost_usd_micros"
# 每次调用的默认 deadline（秒）：容得下网关的三次尝试（go-model-gateway 的 gateway.DefaultAttemptTimeout 是 90 秒）。
DEFAULT_TIMEOUT_S = 300.0
# 在 activity 里调用时，deadline 比 activity 剩下的时间再早这么多秒：留给错误回传与 activity 收尾。
ACTIVITY_MARGIN_S = 2.0

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "ENV_MODEL_GATEWAY_ADDRESS",
    "ERROR_TRAILER",
    "RETRY_AFTER_DETAIL",
    "GrpcModelGateway",
    "MissingGatewayAddressError",
    "from_env",
]

_BY_STATUS: dict[grpc.StatusCode, type[ProviderError]] = {
    grpc.StatusCode.UNAVAILABLE: TransientError,
    grpc.StatusCode.DEADLINE_EXCEEDED: TransientError,
    grpc.StatusCode.RESOURCE_EXHAUSTED: TransientError,
    grpc.StatusCode.INVALID_ARGUMENT: InvalidError,
    grpc.StatusCode.PERMISSION_DENIED: DeniedError,
    grpc.StatusCode.UNAUTHENTICATED: DeniedError,
    grpc.StatusCode.NOT_FOUND: NotFoundError,
}


class MissingGatewayAddressError(InvalidError, ValueError):
    """AR_MODEL_GATEWAY_ADDRESS 缺失或为空（from_env 抛出）。

    它是 InvalidError（约定 5），同时也是 ValueError：评测命令行（py-learning 的 evals.__main__）加载被测对象时
    只接 ValueError，按参数错误以退出码 2 报出；作为 AR_EVAL_GATEWAY_FACTORY 时，缺地址因此报成一行提示，不是调用栈。
    """


def _retry_after(error: error_pb2.Error) -> timedelta | None:
    field = error.details.fields.get(RETRY_AFTER_DETAIL)
    if field is None or field.WhichOneof("kind") != "number_value" or field.number_value < 0:
        return None
    return timedelta(milliseconds=field.number_value)


def _from_trailer(raw: Any) -> ProviderError | None:
    """解析 trailer；解析不了、类别为 UNSPECIFIED 或不认识时返回 None。"""
    try:
        error = error_pb2.Error.FromString(bytes(raw))
        category = ErrorCategory(error.category)
    except (DecodeError, ValueError, TypeError):
        return None
    retry_after = _retry_after(error) if category is ErrorCategory.TRANSIENT else None
    message = error.message
    last = error.details.fields.get(LAST_ERROR_CODE_DETAIL)
    if last is not None and last.WhichOneof("kind") == "string_value" and last.string_value:
        message = f"{message}（最后一次失败：{last.string_value}）"
    restored = ProviderError.of(category, error.code or "gateway_error", message, retry_after=retry_after)
    billed = error.details.fields.get(BILLED_COST_DETAIL)
    if billed is not None and billed.WhichOneof("kind") == "number_value" and billed.number_value > 0:
        with_billed_cost(restored, int(billed.number_value))
    return restored


def restore_error(exc: grpc.aio.AioRpcError) -> ProviderError:
    """把 grpc 的错误还原成 ProviderError（规则见模块说明）。错误信息只用 trailer 里的说明或状态码名，不用 details 文本。"""
    for key, value in exc.trailing_metadata() or ():
        if key == ERROR_TRAILER:
            restored = _from_trailer(value)
            if restored is not None:
                return restored
            break
    status = exc.code()
    name = status.name.lower() if isinstance(status, grpc.StatusCode) else "unknown"
    cls = _BY_STATUS.get(status, TransientError)
    return cls(f"grpc_{name}", f"调用 Model Gateway 失败（gRPC {name}）")


class GrpcModelGateway:
    """到 go-model-gateway 的 gRPC 客户端，实现 ModelGateway Protocol。

    address 形如 127.0.0.1:8091（h2c，不加密；服务间认证留给部署层的 mTLS）。timeout_s 是每次调用的 deadline 上限（秒），
    在 activity 里还受 activity 剩下的时间限制（见模块说明）。

    构造时不建连接、不需要事件循环：评测的工厂（from_env）与 Worker 的组装可能在事件循环之外调用。
    通道在第一次调用时按当时运行的事件循环创建（事件循环换了就重建）；aclose() 关闭通道，没建过通道时什么也不做，可以重复调用。
    """

    def __init__(self, address: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.address = address
        self.timeout_s = timeout_s
        self._channel: grpc.aio.Channel | None = None
        self._stub: mg_grpc.ModelGatewayServiceStub | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def __repr__(self) -> str:
        return f"GrpcModelGateway({self.address!r})"

    def _client(self) -> mg_grpc.ModelGatewayServiceStub:
        loop = asyncio.get_running_loop()
        if self._stub is None or self._loop is not loop:
            # grpc.aio 的通道绑定创建它的事件循环；换了事件循环（例如评测里多次 asyncio.run）就丢掉旧的。
            self._channel = grpc.aio.insecure_channel(self.address)
            self._stub = mg_grpc.ModelGatewayServiceStub(self._channel)
            self._loop = loop
        return self._stub

    def _deadline(self) -> float:
        """这次调用的 deadline（秒）：timeout_s；在 activity 里时不超过 activity 剩下的时间减去 ACTIVITY_MARGIN_S。"""
        timeout = self.timeout_s
        if activity.in_activity():
            info = activity.info()
            if info.start_to_close_timeout is not None:
                elapsed = (datetime.now(UTC) - info.started_time).total_seconds()
                timeout = min(timeout, info.start_to_close_timeout.total_seconds() - elapsed - ACTIVITY_MARGIN_S)
        if timeout <= 0:
            raise TransientError("activity_deadline", "activity 剩下的时间不够再调用一次模型")
        return timeout

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        """调用 ModelGatewayService.Generate；请求与响应原样传递。失败抛 ProviderError（见模块说明）。"""
        timeout = self._deadline()
        try:
            return await self._client().Generate(request, timeout=timeout)
        except grpc.aio.AioRpcError as exc:
            raise restore_error(exc) from None

    async def stream_generate(self, request: mg.GenerateRequest, on_delta: Callable[[str, str], None] | None = None) -> mg.GenerateResponse:
        """调用 ModelGatewayService.StreamGenerate（N-19/N-20）：text_delta、thinking_delta 依次交给 on_delta（"text" 或 "thinking"，片段），
        返回最后一个分片里的完整结果（与 Generate 的响应相同，usage 取分片上的）。网关只在第一个分片之前重试与切换模型；
        之后出错直接结束流，这里抛 ProviderError；没有最后一个分片就结束的按 transient 处理。"""
        timeout = self._deadline()
        stream_request = mg.StreamGenerateRequest(tenant=request.tenant, call=request.call, generation=request.generation)
        try:
            async for chunk in self._client().StreamGenerate(stream_request, timeout=timeout):
                kind = chunk.WhichOneof("chunk")
                if kind == "final":
                    final = mg.GenerateResponse()
                    final.CopyFrom(chunk.final)
                    if chunk.HasField("usage") and not final.HasField("usage"):
                        final.usage.CopyFrom(chunk.usage)
                    return final
                if on_delta is not None and kind in ("text_delta", "thinking_delta"):
                    on_delta("text" if kind == "text_delta" else "thinking", getattr(chunk, kind))
        except grpc.aio.AioRpcError as exc:
            raise restore_error(exc) from None
        raise TransientError("stream_incomplete", "流式生成没有给出最后一个分片就结束了")

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        """调用 ModelGatewayService.Embed；请求与响应原样传递。失败抛 ProviderError（见模块说明）。"""
        timeout = self._deadline()
        try:
            return await self._client().Embed(request, timeout=timeout)
        except grpc.aio.AioRpcError as exc:
            raise restore_error(exc) from None

    async def aclose(self) -> None:
        """关闭通道；没建过通道时什么也不做，可以重复调用。"""
        channel, self._channel, self._stub, self._loop = self._channel, None, None, None
        if channel is not None:
            await channel.close()


def from_env(env: Mapping[str, str] | None = None) -> GrpcModelGateway:
    """按环境变量 AR_MODEL_GATEWAY_ADDRESS 构造客户端；env 为 None 时读 os.environ。

    地址缺失或只有空白时抛 MissingGatewayAddressError（InvalidError，也是 ValueError；错误信息写出变量名），
    不悄悄换成桩模型。不带参数调用即可作为 AR_EVAL_GATEWAY_FACTORY。
    """
    address = (os.environ if env is None else env).get(ENV_MODEL_GATEWAY_ADDRESS, "").strip()
    if not address:
        raise MissingGatewayAddressError("missing_gateway_address", f"没有设置 {ENV_MODEL_GATEWAY_ADDRESS}（Model Gateway 的 host:port）")
    return GrpcModelGateway(address)
