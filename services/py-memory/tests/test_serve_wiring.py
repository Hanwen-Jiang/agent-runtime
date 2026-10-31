"""docs/plan/memory-live.md M-01、M-02 验收：Memory Service 启动时选向量库与 embedder。生产配置不全就拒绝启动，不悄悄退回开发实现。"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from agentruntime.common.v1 import tenant_pb2  # noqa: F401
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.dev.vector_store import InMemoryVectorStoreProvider

from agentruntime_memory.embedding import GatewayEmbedder, HashingEmbedder
from agentruntime_memory.wiring import ConfigError, embedder, vector_store

from .memory_support import ALICE

KEYS: Any = object()
VERSION = "a6api/text-embedding-3-large@2026-10"


def fake_adapter(monkeypatch: pytest.MonkeyPatch, module: str, cls: str) -> list[dict[str, Any]]:
    made: list[dict[str, Any]] = []

    def build(**kwargs: Any) -> Any:
        made.append(kwargs)
        return SimpleNamespace(kind=cls)

    monkeypatch.setitem(sys.modules, f"agentruntime_core.adapters.{module}", SimpleNamespace(**{cls: build}))
    return made


def test_milvus_is_chosen_with_the_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    made = fake_adapter(monkeypatch, "milvus", "MilvusVectorStoreProvider")
    assert vector_store({"AR_MILVUS_URI": "http://milvus:19530"}, KEYS).kind == "MilvusVectorStoreProvider"
    assert made[0]["keys"] is KEYS, "向量按租户密钥变换"
    assert made[0]["uri"] == "http://milvus:19530"


def test_qdrant_still_works_until_it_is_retired(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_adapter(monkeypatch, "qdrant", "QdrantVectorStoreProvider")
    assert vector_store({"AR_QDRANT_URL": "http://qdrant:6333"}, KEYS).kind == "QdrantVectorStoreProvider"


def test_both_stores_configured_is_refused() -> None:
    with pytest.raises(ConfigError):
        vector_store({"AR_MILVUS_URI": "http://m", "AR_QDRANT_URL": "http://q"}, KEYS)


def test_production_without_a_vector_store_is_refused() -> None:
    """内存实现重启就丢：有业务库而向量在内存里，重启后两边对不上。"""
    with pytest.raises(ConfigError):
        vector_store({"AR_RELATIONAL_DSN": "postgres://x"}, KEYS)


def test_development_may_use_the_in_memory_store() -> None:
    assert isinstance(vector_store({}, KEYS), InMemoryVectorStoreProvider)


def test_development_uses_the_hashing_embedder() -> None:
    assert isinstance(embedder({}), HashingEmbedder)


@pytest.mark.parametrize("partial", [{}, {"AR_MODEL_GATEWAY_ADDRESS": "gw:8091"}, {"AR_EMBEDDING_VERSION": VERSION}])
def test_production_never_falls_back_to_hashing(partial: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        embedder({"AR_RELATIONAL_DSN": "postgres://x", **partial})


def test_half_configured_embedding_is_refused_in_development_too() -> None:
    with pytest.raises(ConfigError):
        embedder({"AR_MODEL_GATEWAY_ADDRESS": "gw:8091"})


async def test_the_gateway_embedder_sends_version_and_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[mg.EmbedRequest] = []

    class Gateway:
        def __init__(self, address: str) -> None:
            self.address = address

        async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
            seen.append(request)
            return mg.EmbedResponse(embedding_version=VERSION, embeddings=[mg.Embedding(values=[0.1, 0.2])])

    monkeypatch.setattr("agentruntime_memory.wiring.GatewayClient", Gateway)
    built = embedder({"AR_MODEL_GATEWAY_ADDRESS": "gw:8091", "AR_EMBEDDING_VERSION": VERSION})
    assert isinstance(built, GatewayEmbedder) and built.version == VERSION
    vectors = await built.embed(ALICE, ["退款"])
    assert len(vectors) == 1 and seen[0].embedding_version == VERSION and seen[0].tenant.tenant_id == ALICE.tenant_id
