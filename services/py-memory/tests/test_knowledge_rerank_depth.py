"""Opt-in top-eight CPU reranking depth applies to both authorized retrieval channels."""
from pathlib import Path

import pytest

from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.vector_store import InMemoryVectorStoreProvider
from agentruntime_memory.embedding import HashingEmbedder
from agentruntime_memory.identity import Identity
from agentruntime_memory.knowledge import KnowledgeService
from agentruntime_memory.retrieval import RetrievalEngine
from agentruntime_memory.wiring import ConfigError, knowledge_rerank_top8_depth


@pytest.mark.parametrize("value", ["0", "41", "-1", "six", "6.5"])
def test_rerank_depth_rejects_invalid_configuration(value):
    with pytest.raises(ConfigError, match="AR_KNOWLEDGE_RERANK_TOP8_DEPTH"):
        knowledge_rerank_top8_depth({"AR_KNOWLEDGE_RERANK_TOP8_DEPTH":value})


async def test_service_search_candidate_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    user = Identity("probe", "alice", "workspace")
    vectors = InMemoryVectorStoreProvider()
    sql = SQLiteRelationalStoreProvider(tmp_path / "db")
    ids = {f"ver_{i}" for i in range(48)}
    received = []

    async def authorize(identity, collection, generation, requested):
        return set(requested) & ids

    async def rerank(identity, query, passages):
        received.append(len(passages))
        return [float(i) for i in range(len(passages))]

    embedding = HashingEmbedder()
    depths = []
    bound_type = type(vectors.bind(user.tenant_context()))
    original_query = bound_type.query

    async def trace_query(self, *args, **kwargs):
        depths.append(kwargs["top_k"])
        return await original_query(self, *args, **kwargs)

    monkeypatch.setattr(bound_type, "query", trace_query)
    configured = knowledge_rerank_top8_depth({"AR_KNOWLEDGE_RERANK_TOP8_DEPTH": "6"})
    assert knowledge_rerank_top8_depth({}) == 40
    shared = dict(vectors=vectors, embedder=embedding, relational=sql, authorize=authorize)
    narrow = KnowledgeService(RetrievalEngine(**shared, rerank=rerank, rerank_top8_depth=configured))
    wide = KnowledgeService(RetrievalEngine(**shared, rerank=rerank))
    hybrid = KnowledgeService(RetrievalEngine(**shared, rerank_top8_depth=configured))
    await narrow.engine.setup()
    params = {"collection_id": "collection", "generation": "g1"}
    docs = [{"version_id": v, "representation_id": "rep_" + v, "blocks": [
        {"id": "block", "text": "Refund receipt deadline for purchase " + v, "anchor": {"page_index": 0}}
    ]} for v in sorted(ids)]
    try:
        await narrow.execute(user, "index", {**params, "documents": docs})
        search = {**params, "query": "refund receipt", "embedding_version": embedding.version}
        for service, limit, depth in [(narrow, 8, 6), (wide, 8, 40), (narrow, 20, 40)]:
            depths.clear()
            result = await service.execute(user, "search", {**search, "limit": limit})
            assert depths == [depth, depth]
            assert 0 < received[-1] <= 2 * depth
            assert result["retrieval"]["candidate_limit"] == depth * 2
            assert result["retrieval"]["top_k"] == depth
            assert 0 < len(result["hits"]) <= limit
        calls = len(received)
        out = await hybrid.execute(user, "search", {**search, "limit": 8})
        assert len(received) == calls and out["retrieval"]["candidate_limit"] == 40
        assert out["retrieval"]["reranked"] is False
    finally:
        await sql.aclose()


async def test_serve_wires_configured_depth_into_knowledge_handler(tmp_path: Path, monkeypatch):
    from unittest.mock import AsyncMock
    from agentruntime_memory import __main__ as entry

    for key in ("AR_RELATIONAL_DSN", "AR_MILVUS_URI", "AR_QDRANT_URL", "AR_SECRETS_FILE", "AR_SECRETS_SEED_FILE", "AR_MEMORY_RERANK_MODE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "AR_DATA_DIR": str(tmp_path), "AR_DEV_SIGNING_KEY": "test-only", "AR_MODEL_GATEWAY_ADDRESS": "127.0.0.1:1",
        "AR_EMBEDDING_VERSION": "fixture", "AR_KNOWLEDGE_RERANK": "1", "AR_KNOWLEDGE_RERANK_TOP8_DEPTH": "6",
    }.items():
        monkeypatch.setenv(key, value)
    start = AsyncMock(return_value=(AsyncMock(), 0))
    monkeypatch.setattr(entry, "start_server", start)
    await entry.serve()
    engine = start.await_args.kwargs["knowledge"].engine
    assert engine.rerank is not None
    assert engine.rerank_top8_depth == 6
