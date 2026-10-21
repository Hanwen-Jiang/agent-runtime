"""MemoryService 的 gRPC 客户端。token 按请求的租户上下文给出令牌（Worker 用签名密钥按 Task 的租户签短期令牌）。

submit_candidates 的签名与 Worker 收尾写回用的 MemoryClient 相同，可以直接替换开发用的 RecordingMemoryClient。
"""

from __future__ import annotations

from collections.abc import Callable

import grpc

from agentruntime.common.v1 import tenant_pb2
from agentruntime.memory.v1 import memory_pb2, memory_pb2_grpc


class MemoryServiceClient:
    def __init__(self, address: str, token: Callable[[tenant_pb2.TenantContext], str]) -> None:
        self.address = address
        self.token = token
        self._channel = grpc.aio.insecure_channel(address)
        self._stub = memory_pb2_grpc.MemoryServiceStub(self._channel)

    def _metadata(self, tenant: tenant_pb2.TenantContext) -> tuple[tuple[str, str], ...]:
        return (("authorization", f"Bearer {self.token(tenant)}"),)

    async def submit_candidates(self, request: memory_pb2.SubmitCandidatesRequest) -> memory_pb2.SubmitCandidatesResponse:
        return await self._stub.SubmitCandidates(request, metadata=self._metadata(request.tenant))

    async def search(self, request: memory_pb2.SearchRequest) -> memory_pb2.SearchResponse:
        return await self._stub.Search(request, metadata=self._metadata(request.tenant))

    async def record_feedback(self, request: memory_pb2.RecordFeedbackRequest) -> memory_pb2.RecordFeedbackResponse:
        return await self._stub.RecordFeedback(request, metadata=self._metadata(request.tenant))

    async def delete(self, request: memory_pb2.DeleteRequest) -> memory_pb2.DeleteResponse:
        return await self._stub.Delete(request, metadata=self._metadata(request.tenant))

    async def aclose(self) -> None:
        await self._channel.close()
