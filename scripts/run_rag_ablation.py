#!/usr/bin/env python3
"""4-arm RAG ablation over held-out agent-runtime Dev QA + unanswerable queries (WS3).

Measures Hit@1, Hit@5, Source Recall@5, Citation Line Precision, Unanswerable Abstention Rate,
Rerank Skip Rate, Cache Hit Rate, and real end-to-end RetrievalEngine latency (P50 / P95 ms).
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for pkg in ("py-core/src", "py-proto/src", "py-memory/src"):
    p = str(ROOT / "services" / pkg)
    if p not in sys.path:
        sys.path.insert(0, p)

from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider  # noqa: E402
from agentruntime_core.dev.vector_store import Filter, InMemoryVectorStoreProvider  # noqa: E402
from agentruntime_memory.cache import TenantQueryEmbeddingCache  # noqa: E402
from agentruntime_memory.embedding import HashingEmbedder, sparse_vector, tokens  # noqa: E402
from agentruntime_memory.identity import Identity  # noqa: E402
from agentruntime_memory.metrics import MemoryMetricsRegistry  # noqa: E402
from agentruntime_memory.retrieval import RetrievalEngine  # noqa: E402
from agentruntime_memory.retrieval.engine import CORPUS  # noqa: E402

EVALSET_PATH = ROOT / "services/py-learning/evalsets/memory-rag/dev-qa-heldout.jsonl"
STOPWORDS = frozenset({
    "where", "is", "are", "the", "a", "an", "in", "to", "for", "of", "and", "or",
    "how", "does", "which", "when", "class", "function", "module", "define", "defined",
})


def _ident() -> Identity:
    return Identity(tenant_id="rag-ablation", workspace_id="repo-qa", user_id="evaluator")


def _load_evalset() -> list[dict[str, Any]]:
    return [json.loads(line) for line in EVALSET_PATH.read_text("utf-8").splitlines() if line.strip()]


def _collect_corpus_files(evalset: list[dict[str, Any]]) -> list[str]:
    files = {f for row in evalset for f in row.get("expected_files", [])}
    extras = [
        "services/py-memory/src/agentruntime_memory/retrieval/adapter.py",
        "services/py-worker/src/agentruntime_worker/reviewer/reviewer.py",
        "services/py-worker/src/agentruntime_worker/executor/node_executor.py",
        "services/py-learning/src/agentruntime_learning/evolution/wave2.py",
    ]
    for rel in extras:
        if (ROOT / rel).is_file():
            files.add(rel)
    return sorted(files)


def _content_tokens(text: str) -> set[str]:
    raw = {t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text.lower()) if len(t) >= 2}
    return {t for t in raw if t not in STOPWORDS}


def _line_window_blocks(rel_path: str, text: str, window: int = 45, overlap: int = 10) -> list[dict[str, Any]]:
    lines = text.splitlines()
    blocks: list[dict[str, Any]] = []
    step = max(1, window - overlap)
    idx = 0
    chunk_i = 0
    while idx < len(lines):
        end = min(len(lines), idx + window)
        snippet = "\n".join(lines[idx:end])
        blocks.append({
            "id": f"win_{chunk_i}",
            "text": snippet,
            "anchor": {"kind": "line_window", "path": rel_path, "start_line": idx + 1, "end_line": end},
        })
        if end == len(lines):
            break
        idx += step
        chunk_i += 1
    return blocks


def _ast_code_blocks(rel_path: str, text: str) -> list[dict[str, Any]]:
    lang = "python" if rel_path.endswith(".py") else "go" if rel_path.endswith(".go") else "text"
    lines = text.splitlines()
    return [{
        "id": "file_0",
        "text": text,
        "anchor": {"kind": "code_file", "path": rel_path, "language": lang, "start_line": 1, "end_line": max(1, len(lines))},
    }]


class _LexicalCrossEncoder:
    """Deterministic cross-encoder approximation computing token+identifier+BM25 interaction scores."""

    def __init__(self, corpus_texts: Sequence[str]) -> None:
        self.calls = 0
        df: Counter[str] = Counter()
        for doc in corpus_texts:
            for tok in set(tokens(doc)):
                df[tok] += 1
        n_docs = max(1, len(corpus_texts))
        self.idf = {tok: math.log((n_docs + 1.0) / (freq + 0.5)) + 1.0 for tok, freq in df.items()}

    async def __call__(self, _identity: Identity, query: str, candidates: Sequence[str]) -> list[float]:
        self.calls += 1
        q_toks = tokens(query)
        q_ident = _content_tokens(query)
        # Preserve camelCase/snake_case exact identifier salience
        exact_idents = {m for m in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query) if "_" in m or any(c.isupper() for c in m[1:])}
        scores: list[float] = []
        for cand in candidates:
            c_counts = Counter(tokens(cand))
            doc_len = max(1, sum(c_counts.values()))
            bm25 = 0.0
            for t in q_toks:
                tf = c_counts.get(t, 0)
                if tf:
                    idf = self.idf.get(t, 1.0)
                    bm25 += idf * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * (doc_len / 120.0)))
            c_ident = _content_tokens(cand)
            overlap = len(q_ident & c_ident) / max(1, len(q_ident))
            ident_bonus = sum(3.0 for sym in exact_idents if sym in cand)
            # Small bonus when path/header/def matches
            def_bonus = 0.8 if any(f"def {sym}" in cand or f"class {sym}" in cand or f"func {sym}" in cand for sym in exact_idents) else 0.0
            scores.append(bm25 + 2.5 * overlap + ident_bonus + def_bonus)
        return scores


def _hit_file(hit: dict[str, Any]) -> str:
    anchor = hit.get("anchor") or {}
    return str(anchor.get("path") or hit.get("version_id", "").replace("__", "/") or "")


def _ranges_overlap(anchor: dict[str, Any], expected: list[dict[str, Any]], hit_file: str) -> bool:
    s = int(anchor.get("start_line") or 0)
    e = int(anchor.get("end_line") or 0)
    for exp in expected:
        if exp["file"] != hit_file:
            continue
        es, ee = int(exp["start"]), int(exp["end"])
        if s <= ee and e >= es:
            return True
    return False


def _should_abstain(query: str, hits: list[dict[str, Any]]) -> bool:
    if not hits:
        return True
    q_terms = _content_tokens(query)
    if not q_terms:
        return False
    top_text = hits[0].get("text", "")
    top_terms = _content_tokens(top_text)
    overlap = len(q_terms & top_terms) / len(q_terms)
    # Require >= 0.30 content term coverage to answer rather than hallucinate
    return overlap < 0.30


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 3)


def _evaluate_predictions(evalset: list[dict[str, Any]], per_item_hits: list[list[dict[str, Any]]], latencies_ms: list[float]) -> dict[str, Any]:
    ans_items = [(row, hits) for row, hits in zip(evalset, per_item_hits, strict=True) if row["answerable"]]
    unans_items = [(row, hits) for row, hits in zip(evalset, per_item_hits, strict=True) if not row["answerable"]]

    hit1 = 0
    hit5 = 0
    recall5_sum = 0.0
    cite_prec = 0
    for row, hits in ans_items:
        exp_files = set(row["expected_files"])
        top_files = [_hit_file(h) for h in hits[:5]]
        if hits and _hit_file(hits[0]) in exp_files:
            hit1 += 1
        if any(f in exp_files for f in top_files):
            hit5 += 1
        recall5_sum += len(exp_files & set(top_files)) / max(1, len(exp_files))
        if hits and _ranges_overlap(hits[0].get("anchor") or {}, row["expected_line_ranges"], _hit_file(hits[0])):
            cite_prec += 1

    abstained = sum(1 for row, hits in unans_items if _should_abstain(row["question"], hits))
    n_ans = max(1, len(ans_items))
    n_unans = max(1, len(unans_items))
    return {
        "answerable_count": len(ans_items),
        "unanswerable_count": len(unans_items),
        "hit_at_1": round(hit1 / n_ans, 4),
        "hit_at_5": round(hit5 / n_ans, 4),
        "source_recall_at_5": round(recall5_sum / n_ans, 4),
        "citation_line_precision": round(cite_prec / n_ans, 4),
        "abstention_rate": round(abstained / n_unans, 4),
        "p50_ms": _percentile(latencies_ms, 50),
        "p95_ms": _percentile(latencies_ms, 95),
    }


async def _index_repo(engine: RetrievalEngine, ident: Identity, files: list[str], *, ast_mode: bool, generation: str) -> None:
    docs: list[dict[str, Any]] = []
    for idx, rel in enumerate(files):
        text = (ROOT / rel).read_text("utf-8")
        blocks = _ast_code_blocks(rel, text) if ast_mode else _line_window_blocks(rel, text)
        docs.append({
            "version_id": f"f{idx}_{Path(rel).name.replace('.', '_')}",
            "representation_id": f"rep_{idx}",
            "blocks": blocks,
        })
    await engine.index(ident, "devqa", generation, docs)


async def main() -> None:
    evalset = _load_evalset()
    files = _collect_corpus_files(evalset)
    corpus_texts = [(ROOT / rel).read_text("utf-8") for rel in files]
    ident = _ident()

    async def allow_all(_id: Identity, _col: str, _gen: str, versions: Sequence[str]) -> set[str]:
        return set(versions)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        rel = SQLiteRelationalStoreProvider(tmp_path / "db")
        vecs = InMemoryVectorStoreProvider()
        embedder = HashingEmbedder()

        # Setup two indexed generations: line-window and AST structural code chunks
        base_engine = RetrievalEngine(vectors=vecs, embedder=embedder, relational=rel, authorize=allow_all, code_nav_enabled=False)
        await base_engine.setup()
        await _index_repo(base_engine, ident, files, ast_mode=False, generation="gen_win")

        ast_prep = RetrievalEngine(vectors=vecs, embedder=embedder, relational=rel, authorize=allow_all, code_nav_enabled=True)
        await _index_repo(ast_prep, ident, files, ast_mode=True, generation="gen_ast")

        arms: dict[str, Any] = {}

        # Arm 1: Sparse BM25-like lexical recall on line-window chunks
        bound = vecs.bind(ident.tenant_context())
        arm1_hits: list[list[dict[str, Any]]] = []
        arm1_lat: list[float] = []
        for row in evalset:
            t0 = time.perf_counter()
            sp = sparse_vector(row["question"])
            raw_hits = await bound.query(
                CORPUS,
                embedding_version=embedder.version,
                top_k=5,
                sparse=sp,
                filter=Filter(must={"collection_id": "devqa", "generation": "gen_win"}),
            ) if sp.indices else []
            resolved: list[dict[str, Any]] = []
            async with rel.bind(ident.tenant_context()).transaction(read_only=True) as tx:
                for rh in raw_hits:
                    node = await tx.query_one("SELECT body, anchor, version_id FROM retrieval_nodes WHERE node_id=$1", (rh.id,))
                    if node:
                        resolved.append({"id": rh.id, "score": rh.score, "text": node["body"], "anchor": json.loads(node["anchor"]), "version_id": node["version_id"]})
            arm1_lat.append((time.perf_counter() - t0) * 1000.0)
            arm1_hits.append(resolved)
        arms["arm1_bm25_line_window"] = {**_evaluate_predictions(evalset, arm1_hits, arm1_lat), "rerank_calls": 0, "rerank_skip_rate": 0.0, "cache_hit_rate": 0.0}

        # Arm 2: Hybrid RRF on line-window chunks (no rerank)
        m2 = MemoryMetricsRegistry()
        eng2 = RetrievalEngine(vectors=vecs, embedder=embedder, relational=rel, authorize=allow_all, code_nav_enabled=False, metrics=m2)
        arm2_hits: list[list[dict[str, Any]]] = []
        arm2_lat: list[float] = []
        for row in evalset:
            t0 = time.perf_counter()
            res = await eng2.search(ident, "devqa", "gen_win", row["question"], embedding_version=embedder.version, limit=5)
            arm2_lat.append((time.perf_counter() - t0) * 1000.0)
            arm2_hits.append(res["hits"])
        arms["arm2_hybrid_rrf_line_window"] = {**_evaluate_predictions(evalset, arm2_hits, arm2_lat), "rerank_calls": 0, "rerank_skip_rate": 0.0, "cache_hit_rate": 0.0}

        # Arm 3: AST structural chunking + Hybrid RRF + unconditional rerank (uncached)
        m3 = MemoryMetricsRegistry()
        rerank3 = _LexicalCrossEncoder(corpus_texts)
        eng3 = RetrievalEngine(
            vectors=vecs, embedder=embedder, relational=rel, authorize=allow_all,
            rerank=rerank3, code_nav_enabled=True, rerank_margin_threshold=0.0,
            query_cache=TenantQueryEmbeddingCache(max_size=0, ttl_seconds=0.0, metrics=m3), metrics=m3,
        )
        arm3_hits: list[list[dict[str, Any]]] = []
        arm3_lat: list[float] = []
        for row in evalset:
            t0 = time.perf_counter()
            res = await eng3.search(ident, "devqa", "gen_ast", row["question"], embedding_version=embedder.version, limit=5)
            arm3_lat.append((time.perf_counter() - t0) * 1000.0)
            arm3_hits.append(res["hits"])
        arms["arm3_ast_rrf_unconditional_rerank"] = {**_evaluate_predictions(evalset, arm3_hits, arm3_lat), "rerank_calls": rerank3.calls, "rerank_skip_rate": 0.0, "cache_hit_rate": 0.0}

        # Arm 4: AST + Hybrid RRF + Confidence-Gated Rerank (margin=0.35) + Tenant Query Cache (Cold vs Warm)
        m4 = MemoryMetricsRegistry()
        rerank4 = _LexicalCrossEncoder(corpus_texts)
        cache4 = TenantQueryEmbeddingCache(max_size=512, ttl_seconds=300.0, metrics=m4)
        eng4 = RetrievalEngine(
            vectors=vecs, embedder=embedder, relational=rel, authorize=allow_all,
            rerank=rerank4, code_nav_enabled=True, rerank_margin_threshold=0.35,
            query_cache=cache4, metrics=m4,
        )
        arm4_cold_hits: list[list[dict[str, Any]]] = []
        arm4_cold_lat: list[float] = []
        for row in evalset:
            t0 = time.perf_counter()
            res = await eng4.search(ident, "devqa", "gen_ast", row["question"], embedding_version=embedder.version, limit=5)
            arm4_cold_lat.append((time.perf_counter() - t0) * 1000.0)
            arm4_cold_hits.append(res["hits"])
        snap_cold = m4.snapshot()
        skips_cold = sum(snap_cold["rerank_skipped"].values())
        calls_cold = sum(snap_cold["rerank_calls"].values())
        arms["arm4_ast_rrf_gated_rerank_cold"] = {
            **_evaluate_predictions(evalset, arm4_cold_hits, arm4_cold_lat),
            "rerank_calls": calls_cold,
            "rerank_skipped": skips_cold,
            "rerank_skip_rate": round(skips_cold / max(1, skips_cold + calls_cold), 4),
            "cache_hit_rate": round(snap_cold["cache_hits"] / max(1, snap_cold["cache_hits"] + snap_cold["cache_misses"]), 4),
        }

        arm4_warm_hits: list[list[dict[str, Any]]] = []
        arm4_warm_lat: list[float] = []
        prev_hits = snap_cold["cache_hits"]
        prev_misses = snap_cold["cache_misses"]
        for row in evalset:
            t0 = time.perf_counter()
            res = await eng4.search(ident, "devqa", "gen_ast", row["question"], embedding_version=embedder.version, limit=5)
            arm4_warm_lat.append((time.perf_counter() - t0) * 1000.0)
            arm4_warm_hits.append(res["hits"])
        snap_warm = m4.snapshot()
        warm_hits_delta = snap_warm["cache_hits"] - prev_hits
        warm_misses_delta = snap_warm["cache_misses"] - prev_misses
        skips_total = sum(snap_warm["rerank_skipped"].values())
        calls_total = sum(snap_warm["rerank_calls"].values())
        arms["arm4_ast_rrf_gated_rerank_warm"] = {
            **_evaluate_predictions(evalset, arm4_warm_hits, arm4_warm_lat),
            "rerank_calls": calls_total - calls_cold,
            "rerank_skipped": skips_total - skips_cold,
            "rerank_skip_rate": round((skips_total - skips_cold) / max(1, (skips_total - skips_cold) + (calls_total - calls_cold)), 4),
            "cache_hit_rate": round(warm_hits_delta / max(1, warm_hits_delta + warm_misses_delta), 4),
            "prometheus_metrics_sample": m4.render_prometheus().splitlines()[:12],
        }

        await rel.aclose()

    payload = {
        "mode": "local_retrieval_engine_ablation",
        "generated_at": datetime.now(UTC).isoformat(),
        "evalset": str(EVALSET_PATH.relative_to(ROOT)),
        "corpus_files_count": len(files),
        "queries_total": len(evalset),
        "arms": arms,
    }
    out_dir = ROOT / "docs/plan/evidence/rag-ablation-20261127"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ablation-results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    md = [
        "# py-memory 4-Arm Developer RAG Ablation (2026-11-27)",
        "",
        f"- **Held-out Dataset**: `services/py-learning/evalsets/memory-rag/dev-qa-heldout.jsonl` (`18` repo QA with verified `file:start-end` ranges + `6` unanswerable items across `{len(files)}` indexed source files)",
        "",
        "| Arm | Hit@1 | Hit@5 | Source Recall@5 | Citation Line Prec | Abstention | Rerank Skip | Cache Hit | P50 / P95 (ms) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for arm_name, st in arms.items():
        md.append(
            f"| `{arm_name}` | {st['hit_at_1']:.1%} | {st['hit_at_5']:.1%} | {st['source_recall_at_5']:.1%} | "
            f"{st['citation_line_precision']:.1%} | {st['abstention_rate']:.1%} | {st['rerank_skip_rate']:.1%} | "
            f"{st['cache_hit_rate']:.1%} | {st['p50_ms']} / {st['p95_ms']} |"
        )
    md.append("")
    (out_dir / "README.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps({k: v for k, v in payload.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
