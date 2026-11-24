"""Tests for local PyTorch Bi-Encoder embedder and Cross-Encoder reranker."""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace
from typing import Any

import pytest

from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.vector_store import InMemoryVectorStoreProvider
from agentruntime_core.errors import InvalidError

from agentruntime_memory.retrieval import RetrievalEngine
from agentruntime_memory.torch_models import TorchBiEncoderEmbedder, TorchCrossEncoderReranker
from agentruntime_memory.wiring import ConfigError, GatewayReranker, embedder, knowledge_reranker

from .memory_support import ALICE

try:
    import torch
    import torch.nn as nn

    HAS_TORCH = True
except ImportError:
    torch = None  # type: ignore[assignment]
    nn = None  # type: ignore[assignment]
    HAS_TORCH = False


def test_wiring_selects_torch_embedder_and_allows_production() -> None:
    built = embedder({
        "AR_RELATIONAL_DSN": "postgres://prod",
        "AR_EMBEDDER_BACKEND": "torch",
        "AR_TORCH_EMBED_MODEL": "BAAI/bge-small-zh-v1.5",
        "AR_TORCH_DEVICE": "cpu",
        "AR_TORCH_BATCH_SIZE": "16",
        "AR_TORCH_POOLING": "cls",
    })
    assert isinstance(built, TorchBiEncoderEmbedder)
    assert built.version == "torch/BAAI/bge-small-zh-v1.5"
    assert built.config.batch_size == 16
    assert built.pooling == "cls"


def test_wiring_rejects_conflicting_or_incomplete_torch_config() -> None:
    with pytest.raises(ConfigError):
        embedder({"AR_EMBEDDER_BACKEND": "torch"})
    with pytest.raises(ConfigError):
        embedder({
            "AR_EMBEDDER_BACKEND": "torch",
            "AR_TORCH_EMBED_MODEL": "BAAI/bge-m3",
            "AR_MODEL_GATEWAY_ADDRESS": "gw:8091",
        })
    with pytest.raises(ConfigError):
        embedder({"AR_EMBEDDER_BACKEND": "torch", "AR_TORCH_EMBED_MODEL": "BAAI/bge-m3", "AR_TORCH_POOLING": "bad"})
    with pytest.raises(ConfigError):
        knowledge_reranker({"AR_RERANKER_BACKEND": "torch"})
    with pytest.raises(ConfigError):
        knowledge_reranker({"AR_RERANKER_BACKEND": "unknown"})


def test_wiring_selects_torch_or_gateway_reranker() -> None:
    assert knowledge_reranker({}) is None
    torch_rr = knowledge_reranker({
        "AR_RERANKER_BACKEND": "torch",
        "AR_TORCH_RERANK_MODEL": "BAAI/bge-reranker-v2-m3",
        "AR_TORCH_DEVICE": "cpu",
        "AR_TORCH_BATCH_SIZE": "8",
    })
    assert isinstance(torch_rr, TorchCrossEncoderReranker)
    assert torch_rr.config.batch_size == 8

    gw_rr = knowledge_reranker({"AR_MODEL_GATEWAY_ADDRESS": "gw:8091", "AR_KNOWLEDGE_RERANK": "1"})
    assert isinstance(gw_rr, GatewayReranker)


if HAS_TORCH:

    class _MiniTokenizer:
        """Deterministic word-hash tokenizer emitting real PyTorch tensors."""

        vocab = {
            "python": 1,
            "pytorch": 2,
            "reranker": 3,
            "kubernetes": 4,
            "sandbox": 5,
            "firecracker": 6,
            "milvus": 7,
            "bm25": 8,
            "grpc": 9,
            "temporal": 10,
        }

        def _ids(self, text: str) -> list[int]:
            tokens = [self.vocab.get(w.strip(".,?!()").lower(), 11) for w in text.split() if w.strip()]
            return tokens or [11]

        def __call__(
            self,
            texts: list[str],
            text_pairs: list[str] | None = None,
            *,
            padding: bool = True,
            truncation: bool = True,
            max_length: int = 64,
            return_tensors: str = "pt",
        ) -> dict[str, Any]:
            assert padding and truncation and return_tensors == "pt"
            if text_pairs is None:
                seqs = [self._ids(t)[:max_length] for t in texts]
                pair_overlap = None
            else:
                seqs = []
                overlaps = []
                for q, p in zip(texts, text_pairs, strict=True):
                    q_ids = set(self._ids(q)) - {11}
                    p_ids = set(self._ids(p)) - {11}
                    overlaps.append(float(len(q_ids & p_ids)))
                    seqs.append((self._ids(q) + [0] + self._ids(p))[:max_length])
                pair_overlap = torch.tensor(overlaps, dtype=torch.float32)
            width = max(len(s) for s in seqs)
            ids = torch.zeros((len(seqs), width), dtype=torch.long)
            mask = torch.zeros((len(seqs), width), dtype=torch.long)
            for i, s in enumerate(seqs):
                ids[i, : len(s)] = torch.tensor(s, dtype=torch.long)
                mask[i, : len(s)] = 1
            out: dict[str, Any] = {"input_ids": ids, "attention_mask": mask}
            if pair_overlap is not None:
                out["pair_overlap"] = pair_overlap
            return out

    class _MiniBiEncoder(nn.Module):
        def __init__(self, vocab_size: int = 16, hidden: int = 12) -> None:
            super().__init__()
            self.emb = nn.Embedding(vocab_size, hidden)
            with torch.no_grad():
                torch.manual_seed(7)
                self.emb.weight.copy_(torch.randn(vocab_size, hidden))

        def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> Any:
            _ = attention_mask
            return SimpleNamespace(last_hidden_state=self.emb(input_ids))

    class _MiniCrossEncoder(nn.Module):
        def __init__(self, num_labels: int = 1) -> None:
            super().__init__()
            self.num_labels = num_labels
            self.scale = nn.Parameter(torch.tensor(2.0))
            self.bias = nn.Parameter(torch.tensor(-1.0))

        def forward(
            self,
            input_ids: torch.Tensor,
            attention_mask: torch.Tensor,
            pair_overlap: torch.Tensor,
        ) -> Any:
            _ = (input_ids, attention_mask)
            score = pair_overlap * self.scale + self.bias
            if self.num_labels == 1:
                return SimpleNamespace(logits=score.unsqueeze(-1))
            return SimpleNamespace(logits=torch.stack([-score, score], dim=-1))

    class _NanBiEncoder(nn.Module):
        def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> Any:
            _ = attention_mask
            out = torch.full((input_ids.shape[0], input_ids.shape[1], 4), float("nan"))
            return SimpleNamespace(last_hidden_state=out)

    @pytest.mark.parametrize("pooling", ["mean", "cls"])
    async def test_torch_bi_encoder_batches_normalizes_and_runs_concurrently(pooling: str) -> None:
        loads = 0

        def loader() -> tuple[nn.Module, _MiniTokenizer]:
            nonlocal loads
            loads += 1
            return _MiniBiEncoder(), _MiniTokenizer()

        emb = TorchBiEncoderEmbedder(
            "unit/mini-bi-encoder",
            device="cpu",
            batch_size=2,
            pooling=pooling,
            use_fp16=False,
            loader=loader,
        )
        texts = ["pytorch reranker", "kubernetes firecracker sandbox", "milvus bm25", "grpc temporal", "python pytorch"]
        v1, v2 = await asyncio.gather(emb.embed(ALICE, texts), emb.embed(ALICE, texts[:2]))
        assert loads == 1, "lazy loader should initialize the model exactly once"
        assert len(v1) == len(texts) and len(v2) == 2
        for vec in v1:
            assert len(vec) == 12
            norm = math.sqrt(sum(x * x for x in vec))
            assert math.isclose(norm, 1.0, rel_tol=1e-4)
        # Same text yields identical deterministic vector
        assert all(math.isclose(a, b, rel_tol=1e-5) for a, b in zip(v1[0], v2[0], strict=True))

    async def test_torch_bi_encoder_rejects_nan_vectors() -> None:
        emb = TorchBiEncoderEmbedder(
            "unit/nan-bi-encoder",
            device="cpu",
            use_fp16=False,
            loader=lambda: (_NanBiEncoder(), _MiniTokenizer()),
        )
        with pytest.raises(InvalidError, match="non-finite"):
            await emb.embed(ALICE, ["pytorch"])

    @pytest.mark.parametrize("num_labels", [1, 2])
    async def test_torch_cross_encoder_scores_and_ranks_candidates(num_labels: int) -> None:
        reranker = TorchCrossEncoderReranker(
            "unit/mini-cross-encoder",
            device="cpu",
            batch_size=2,
            use_fp16=False,
            loader=lambda: (_MiniCrossEncoder(num_labels=num_labels), _MiniTokenizer()),
        )
        candidates = [
            "kubernetes firecracker sandbox",
            "pytorch reranker python",
            "pytorch milvus bm25",
            "pytorch reranker milvus",
        ]
        scores = await reranker(ALICE, "pytorch reranker", candidates)
        assert len(scores) == len(candidates)
        assert all(0.0 <= s <= 1.0 for s in scores)
        assert scores[1] > scores[2] > scores[0]
        assert scores[3] > scores[0]

    async def test_retrieval_engine_end_to_end_with_torch_embedder_and_reranker(tmp_path: Any) -> None:
        relational = SQLiteRelationalStoreProvider(tmp_path / "memory.db")
        vectors = InMemoryVectorStoreProvider()
        emb = TorchBiEncoderEmbedder(
            "unit/mini-bi-encoder",
            device="cpu",
            batch_size=2,
            use_fp16=False,
            loader=lambda: (_MiniBiEncoder(), _MiniTokenizer()),
        )
        reranker = TorchCrossEncoderReranker(
            "unit/mini-cross-encoder",
            device="cpu",
            batch_size=2,
            use_fp16=False,
            loader=lambda: (_MiniCrossEncoder(num_labels=1), _MiniTokenizer()),
        )

        async def allow_all(_identity: Any, _col: str, _gen: str, versions: Any) -> set[str]:
            return set(versions)

        engine = RetrievalEngine(
            vectors=vectors,
            embedder=emb,
            relational=relational,
            authorize=allow_all,
            rerank=reranker,
        )
        await engine.setup()
        docs = [
            {
                "version_id": "v1",
                "representation_id": "r1",
                "blocks": [{"id": "b1", "text": "kubernetes firecracker sandbox isolation warm pool", "anchor": {"kind": "text"}}],
            },
            {
                "version_id": "v2",
                "representation_id": "r2",
                "blocks": [{"id": "b1", "text": "pytorch reranker python cross encoder scoring", "anchor": {"kind": "text"}}],
            },
            {
                "version_id": "v3",
                "representation_id": "r3",
                "blocks": [{"id": "b1", "text": "grpc temporal workflow durable execution", "anchor": {"kind": "text"}}],
            },
        ]
        staged = await engine.index(ALICE, "col-torch", "gen-1", docs)
        assert staged["status"] == "staged" and staged["chunks"] == 3

        result = await engine.search(ALICE, "col-torch", "gen-1", "pytorch reranker", embedding_version=emb.version, limit=3)
        hits = result["hits"]
        assert hits and result["retrieval"]["reranked"] is True
        assert hits[0]["version_id"] == "v2", "PyTorch cross-encoder should place the exact topic match first"
        await vectors.aclose()
        await relational.aclose()
