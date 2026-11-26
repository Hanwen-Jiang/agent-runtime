"""Tests for py-memory Prometheus /metrics, tenant-isolated embedding cache, and confidence-gated rerank."""

from __future__ import annotations

import asyncio
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.vector_store import InMemoryVectorStoreProvider
from agentruntime_memory.cache import TenantQueryEmbeddingCache
from agentruntime_memory.embedding import HashingEmbedder
from agentruntime_memory.identity import Identity
from agentruntime_memory.metrics import MemoryMetricsRegistry, start_metrics_server
from agentruntime_memory.retrieval import RetrievalEngine
from agentruntime_memory.wiring import embed_cache_config, rerank_margin_threshold, warmup_enabled


def _identity(tenant_id: str, workspace_id: str = "w1", user_id: str = "u1") -> Identity:
    return Identity(tenant_id=tenant_id, workspace_id=workspace_id, user_id=user_id)


@pytest.mark.asyncio
async def test_query_embedding_cache_tenant_isolation_and_ttl() -> None:
    now = [100.0]
    metrics = MemoryMetricsRegistry()
    cache = TenantQueryEmbeddingCache(max_size=2, ttl_seconds=10.0, clock=lambda: now[0], metrics=metrics)
    calls: list[tuple[str, str]] = []

    async def compute(tag: str, query: str) -> list[float]:
        calls.append((tag, query))
        return [1.0, float(len(calls))]

    id_a = _identity("tenant-a", "ws-1")
    id_b = _identity("tenant-b", "ws-1")
    id_a_w2 = _identity("tenant-a", "ws-2")

    v1, hit1 = await cache.get_or_compute(id_a, "v1", "where is auth", lambda: compute("a1", "where is auth"))
    v2, hit2 = await cache.get_or_compute(id_a, "v1", "where is auth", lambda: compute("a1-again", "where is auth"))
    v_b, hit_b = await cache.get_or_compute(id_b, "v1", "where is auth", lambda: compute("b", "where is auth"))
    v_w2, hit_w2 = await cache.get_or_compute(id_a_w2, "v1", "where is auth", lambda: compute("a2", "where is auth"))

    assert hit1 is False
    assert hit2 is True and v2 == v1
    assert hit_b is False and v_b != v1
    assert hit_w2 is False and v_w2 != v1

    # Advance past TTL -> forces recompute even after prior tenant-a hit (tenant-a ws-1 was LRU-evicted or expired)
    now[0] = 120.0
    v_b2, hit_b2 = await cache.get_or_compute(id_b, "v1", "where is auth", lambda: compute("b-expired", "where is auth"))
    assert hit_b2 is False and v_b2 != v_b

    snap = metrics.snapshot()
    assert snap["cache_hits"] == 1
    assert snap["cache_misses"] == 4


@pytest.mark.asyncio
async def test_confidence_gated_rerank_and_metrics_http_endpoint(tmp_path: Path) -> None:
    rel = SQLiteRelationalStoreProvider(tmp_path / "db")
    vecs = InMemoryVectorStoreProvider()
    metrics = MemoryMetricsRegistry()
    rerank_calls = 0

    async def fake_rerank(_identity: Identity, _query: str, texts: Any) -> list[float]:
        nonlocal rerank_calls
        rerank_calls += 1
        return [float(len(texts) - idx) for idx, _ in enumerate(texts)]

    async def allow_all(_identity: Identity, _col: str, _gen: str, versions: Any) -> set[str]:
        return set(versions)

    # Set a tiny positive threshold -> if top1 - top2 >= 0.01 reranker skips; if tied/close it invokes
    engine = RetrievalEngine(
        vectors=vecs,
        embedder=HashingEmbedder(),
        relational=rel,
        authorize=allow_all,
        rerank=fake_rerank,
        rerank_margin_threshold=0.05,
        metrics=metrics,
    )
    await engine.setup()
    ident = _identity("t1")

    docs = [
        {
            "version_id": "v1",
            "representation_id": "r1",
            "blocks": [
                {
                    "id": "b1",
                    "text": "def validate_tool_arguments(name, args, schema):\n    return []\n",
                    "anchor": {"kind": "code_file", "path": "tool_schema.py", "language": "python", "start_line": 1, "end_line": 2},
                }
            ],
        },
        {
            "version_id": "v2",
            "representation_id": "r2",
            "blocks": [
                {
                    "id": "b2",
                    "text": "def unrelated_helper(x):\n    return x + 1\n",
                    "anchor": {"kind": "code_file", "path": "other.py", "language": "python", "start_line": 1, "end_line": 2},
                }
            ],
        },
    ]
    await engine.index(ident, "repo", "gen1", docs)

    # Clear exact-symbol query triggers confident skip without invoking remote/local cross-encoder
    res1 = await engine.search(ident, "repo", "gen1", "validate_tool_arguments", embedding_version="hash-64", limit=5)
    assert res1["hits"]
    assert rerank_calls == 0
    assert res1["retrieval"]["reranked"] is False
    assert res1["retrieval"]["rerank_skipped"] in ("confident_symbol_match", "confident_top1", "single_candidate")

    # With margin threshold disabled (0.0), reranker is always invoked
    engine_always = RetrievalEngine(
        vectors=vecs,
        embedder=HashingEmbedder(),
        relational=rel,
        authorize=allow_all,
        rerank=fake_rerank,
        rerank_margin_threshold=0.0,
        metrics=metrics,
    )
    res2 = await engine_always.search(ident, "repo", "gen1", "helper validate", embedding_version="hash-64", limit=5)
    assert res2["hits"]
    assert rerank_calls == 1
    assert res2["retrieval"]["reranked"] is True

    srv = start_metrics_server("127.0.0.1:0", metrics)
    try:
        port = srv.server_address[1]
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=3).read().decode("utf-8")
        assert 'ar_memory_search_requests_total{scope="knowledge",status="ok"} 2' in body
        assert "ar_memory_search_duration_seconds_count" in body
        assert "ar_memory_rerank_calls_total" in body
        assert "ar_memory_rerank_skipped_total" in body
        assert "ar_memory_embedding_cache_hits_total" in body
        assert "ar_memory_embedding_cache_misses_total" in body
    finally:
        srv.shutdown()
        srv.server_close()
        await rel.aclose()


def test_wiring_env_parsers() -> None:
    assert rerank_margin_threshold({}) == 0.0
    assert rerank_margin_threshold({"AR_MEMORY_RERANK_MARGIN_THRESHOLD": "0.15"}) == pytest.approx(0.15)
    assert embed_cache_config({}) == (512, 300.0)
    assert embed_cache_config({"AR_MEMORY_EMBED_CACHE_MAX_SIZE": "128", "AR_MEMORY_EMBED_CACHE_TTL_SECONDS": "60"}) == (128, 60.0)
    assert warmup_enabled({}) is False
    assert warmup_enabled({"AR_MEMORY_WARMUP": "1"}) is True
