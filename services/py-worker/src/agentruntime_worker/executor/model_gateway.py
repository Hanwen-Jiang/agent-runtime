"""Model Gateway 客户端（设计书 3.3.1）：Python 侧所有模型调用的唯一出口。

请求与响应是 proto/agentruntime/modelgateway/v1 的消息；每个请求的 tenant 字段都是 TenantContext。
生产实现是到 go-model-gateway 的 gRPC 客户端；开发与测试用 activities.stubs.StubModelGateway。
失败一律抛 agentruntime_core.errors 的 ProviderError（transient 可重试，其余不可重试）。

失败之前上游可能已经计费（例如回答不合格、给了请求里没给的工具调用）：网关在错误里带上这次调用已计费的花费，
`billed_cost_usd(exc)` 取出，调用方照样记进成本（TMP 计划第 11.11 节）；成功的回答的 usage 已经含这些花费。
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

BILLED_COST_ATTR = "billed_cost_usd_micros"

_E = TypeVar("_E", bound=BaseException)


class ModelGateway(Protocol):
    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse: ...

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse: ...


def with_billed_cost(exc: _E, cost_usd_micros: int) -> _E:
    """给错误带上失败之前已经计费的花费（微美元）；返回同一个错误。"""
    setattr(exc, BILLED_COST_ATTR, max(int(cost_usd_micros), 0))
    return exc


def billed_cost_usd(exc: BaseException) -> float:
    """错误里带的已计费花费（美元）；没有时为 0。"""
    micros = getattr(exc, BILLED_COST_ATTR, 0)
    return micros / 1_000_000 if isinstance(micros, int) and micros > 0 else 0.0
