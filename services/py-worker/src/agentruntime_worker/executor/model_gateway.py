"""Model Gateway 客户端（设计书 3.3.1）：Python 侧所有模型调用的唯一出口。

请求与响应是 proto/agentruntime/modelgateway/v1 的消息；每个请求的 tenant 字段都是 TenantContext。
生产实现是到 go-model-gateway 的 gRPC 客户端；开发与测试用 activities.stubs.StubModelGateway。
失败一律抛 agentruntime_core.errors 的 ProviderError（transient 可重试，其余不可重试）。
"""

from __future__ import annotations

from typing import Protocol

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg


class ModelGateway(Protocol):
    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse: ...

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse: ...
