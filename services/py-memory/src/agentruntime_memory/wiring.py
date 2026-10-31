"""Memory Service 启动时按环境变量选向量库与 embedder（docs/plan/memory-live.md M-01、M-02）。

生产（设了 AR_RELATIONAL_DSN）配置不全就拒绝启动，不悄悄退回开发实现：向量在内存里重启就丢，哈希嵌入没有语义。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import grpc

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime.modelgateway.v1 import model_gateway_pb2_grpc as mg_grpc
from agentruntime_core.dev.vector_store import InMemoryVectorStoreProvider

from agentruntime_memory.embedding import Embedder, GatewayEmbedder, HashingEmbedder

EMBED_TIMEOUT_S = 30.0


class ConfigError(ValueError):
    """环境变量的组合不对，服务不能这样启动。"""


class GatewayClient:
    """Model Gateway 的 Embed（h2c，服务间认证留给部署层）。通道在第一次调用时建，要在服务的事件循环里。"""

    def __init__(self, address: str) -> None:
        self.address = address
        self._stub: mg_grpc.ModelGatewayServiceStub | None = None

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        if self._stub is None:
            self._stub = mg_grpc.ModelGatewayServiceStub(grpc.aio.insecure_channel(self.address))
        return await self._stub.Embed(request, timeout=EMBED_TIMEOUT_S)


def vector_store(env: Mapping[str, str], keys: Any) -> Any:
    milvus, qdrant = env.get("AR_MILVUS_URI", ""), env.get("AR_QDRANT_URL", "")
    if milvus and qdrant:
        raise ConfigError("AR_MILVUS_URI 与 AR_QDRANT_URL 只能设一个")
    if milvus:
        from agentruntime_core.adapters.milvus import MilvusVectorStoreProvider

        return MilvusVectorStoreProvider(uri=milvus, keys=keys)
    if qdrant:
        from agentruntime_core.adapters.qdrant import QdrantVectorStoreProvider

        return QdrantVectorStoreProvider(url=qdrant, keys=keys)
    if env.get("AR_RELATIONAL_DSN"):
        raise ConfigError("设了 AR_RELATIONAL_DSN 就要设 AR_MILVUS_URI：向量放在内存里，重启后与业务库对不上")
    return InMemoryVectorStoreProvider()


def embedder(env: Mapping[str, str]) -> Embedder:
    address, version = env.get("AR_MODEL_GATEWAY_ADDRESS", ""), env.get("AR_EMBEDDING_VERSION", "")
    if address and version:
        return GatewayEmbedder(GatewayClient(address), version=version)
    if address or version:
        raise ConfigError("AR_MODEL_GATEWAY_ADDRESS 与 AR_EMBEDDING_VERSION 要一起设")
    if env.get("AR_RELATIONAL_DSN"):
        raise ConfigError("设了 AR_RELATIONAL_DSN 就要设 AR_MODEL_GATEWAY_ADDRESS 与 AR_EMBEDDING_VERSION：哈希嵌入只给开发用")
    return HashingEmbedder()
