"""嵌入与稀疏向量（附录 I.6：嵌入文本是标题 + 指导 + 适用条件，不嵌入原始证据）。

- Embedder：真实实现经 Model Gateway 的 Embed（每次调用带租户上下文）；开发与测试用 HashingEmbedder：
  把词与汉字二元组带符号地哈希到固定维度，余弦近似词面重叠，确定、无需模型。
- sparse_vector：稀疏向量（词频，下标是词的 32 位哈希），用于混合召回里的稀疏一路。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.errors import InvalidError
from agentruntime_core.providers.vector_store import SparseVector

from agentruntime_memory.identity import Identity

_TOKEN = re.compile(r"[a-z0-9]+|[㐀-䶿一-鿿]+")


class Embedder(Protocol):
    version: str

    async def embed(self, identity: Identity, texts: Sequence[str]) -> list[tuple[float, ...]]: ...


def _hash(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")


class HashingEmbedder:
    def __init__(self, *, dims: int = 64, version: str = "hash-64") -> None:
        self.dims = dims
        self.version = version

    def _one(self, text: str) -> tuple[float, ...]:
        v = [0.0] * self.dims
        for token, count in Counter(tokens(text)).items():
            h = _hash(token)
            v[h % self.dims] += count if h >> 63 else -count
        norm = math.sqrt(sum(x * x for x in v))
        if norm == 0:
            v[0], norm = 1.0, 1.0
        return tuple(x / norm for x in v)

    async def embed(self, identity: Identity, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [self._one(t) for t in texts]


class GatewayEmbedder:
    """经 Model Gateway 的 Embed：gateway 有 embed(EmbedRequest) 方法（Worker 的 ModelGateway 接口）。"""

    def __init__(self, gateway: Any, *, version: str) -> None:
        self.gateway = gateway
        self.version = version

    async def embed(self, identity: Identity, texts: Sequence[str]) -> list[tuple[float, ...]]:
        response = await self.gateway.embed(mg.EmbedRequest(tenant=identity.tenant_context(), texts=list(texts), embedding_version=self.version))
        if response.embedding_version and response.embedding_version != self.version:
            raise InvalidError("embedding_version_mismatch", "Model Gateway 返回的嵌入版本与配置的不一致")
        if len(response.embeddings) != len(texts):
            raise InvalidError("embedding_count_mismatch", "Model Gateway 返回的嵌入条数与请求不一致")
        return [tuple(e.values) for e in response.embeddings]


def tokens(text: str) -> list[str]:
    """小写的英文词与数字，加汉字二元组（单个汉字成段时取单字）。"""
    out: list[str] = []
    for run in _TOKEN.findall(text.lower()):
        if run.isascii():
            out.append(run)
        elif len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return out


def sparse_vector(text: str) -> SparseVector:
    weights: dict[int, float] = {}
    for token, count in Counter(tokens(text)).items():
        index = _hash(token) & 0xFFFFFFFF
        weights[index] = weights.get(index, 0.0) + float(count)
    pairs = sorted(weights.items())
    return SparseVector(indices=tuple(i for i, _ in pairs), values=tuple(v for _, v in pairs))


def embedding_text(item: Mapping[str, Any]) -> str:
    """标题 + 指导 + 适用条件（任务族、工具）。不含证据、理由与来源。"""
    content = item.get("content") if isinstance(item.get("content"), Mapping) else {}
    applies = item.get("applies_when") if isinstance(item.get("applies_when"), Mapping) else {}
    parts = [str(content.get("title", "")), str(content.get("guidance", ""))]
    conditions = [*applies.get("task_family", []), *applies.get("tools", [])]
    if conditions:
        parts.append(" ".join(str(c) for c in conditions))
    return "\n".join(p for p in parts if p)
