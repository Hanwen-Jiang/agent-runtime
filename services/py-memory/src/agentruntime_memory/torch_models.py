"""Local PyTorch Bi-Encoder embedder and Cross-Encoder reranker for Memory & RAG.

PyTorch and Transformers are imported lazily so default lightweight deployments
do not pay the import or image footprint unless AR_EMBEDDER_BACKEND=torch or
AR_RERANKER_BACKEND=torch is explicitly enabled.
"""

from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agentruntime_core.errors import InvalidError
from agentruntime_memory.identity import Identity


def _require_torch() -> Any:
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError(
            "PyTorch backend requires optional ML dependencies: install `agentruntime-memory[ml]` (torch, transformers)"
        ) from exc
    return torch


def _require_transformers() -> tuple[Any, Any, Any]:
    try:
        from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError(
            "HuggingFace model loading requires `transformers`: install `agentruntime-memory[ml]`"
        ) from exc
    return AutoTokenizer, AutoModel, AutoModelForSequenceClassification


def select_device(requested: str = "auto", *, torch_mod: Any | None = None) -> str:
    """Resolve `auto`, `cuda`, `mps`, or `cpu` into a concrete PyTorch device."""
    torch = torch_mod if torch_mod is not None else _require_torch()
    choice = (requested or "auto").strip().lower()
    if choice == "auto":
        if torch.cuda.is_available():
            return "cuda"
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
        return "cpu"
    if choice in ("cpu", "cuda", "mps") or choice.startswith("cuda:"):
        return choice
    raise ValueError(f"Unsupported PyTorch device: {requested!r}")


def _autocast_context(torch: Any, device: str, use_fp16: bool) -> Any:
    from contextlib import nullcontext

    if not use_fp16:
        return nullcontext()
    if device.startswith("cuda"):
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        return torch.autocast(device_type="cuda", dtype=dtype)
    if device == "mps":
        return torch.autocast(device_type="mps", dtype=torch.float16)
    return nullcontext()


@dataclass(frozen=True)
class TorchModelConfig:
    model_name: str
    device: str = "auto"
    batch_size: int = 32
    max_length: int = 512
    use_fp16: bool = True
    local_files_only: bool = False

    def validate(self) -> None:
        if not self.model_name or not self.model_name.strip():
            raise ValueError("model_name must be non-empty")
        if self.batch_size < 1 or self.batch_size > 1024:
            raise ValueError("batch_size must be between 1 and 1024")
        if self.max_length < 8 or self.max_length > 8192:
            raise ValueError("max_length must be between 8 and 8192")


class TorchBiEncoderEmbedder:
    """Local PyTorch Bi-Encoder implementing the Memory Service `Embedder` protocol.

    Supports mean pooling (default, masked by attention_mask) or CLS pooling,
    L2 normalization, dynamic mini-batching, and non-blocking execution via
    `asyncio.to_thread`.
    """

    def __init__(
        self,
        model_name: str,
        *,
        version: str | None = None,
        device: str = "auto",
        batch_size: int = 32,
        max_length: int = 512,
        pooling: str = "mean",
        normalize: bool = True,
        use_fp16: bool = True,
        local_files_only: bool = False,
        loader: Callable[[], tuple[Any, Any]] | None = None,
    ) -> None:
        if pooling not in ("mean", "cls"):
            raise ValueError("pooling must be 'mean' or 'cls'")
        self.config = TorchModelConfig(
            model_name=model_name,
            device=device,
            batch_size=batch_size,
            max_length=max_length,
            use_fp16=use_fp16,
            local_files_only=local_files_only,
        )
        self.config.validate()
        self.version = version or f"torch/{model_name}"
        self.pooling = pooling
        self.normalize = normalize
        self._loader = loader
        self._lock = threading.Lock()
        self._model: Any = None
        self._tokenizer: Any = None
        self._device: str | None = None

    def _ensure_loaded(self) -> tuple[Any, Any, Any, str]:
        torch = _require_torch()
        if self._model is not None and self._tokenizer is not None and self._device is not None:
            return torch, self._model, self._tokenizer, self._device
        with self._lock:
            if self._model is None or self._tokenizer is None or self._device is None:
                device = select_device(self.config.device, torch_mod=torch)
                if self._loader is not None:
                    model, tokenizer = self._loader()
                else:
                    AutoTokenizer, AutoModel, _ = _require_transformers()
                    tokenizer = AutoTokenizer.from_pretrained(
                        self.config.model_name, local_files_only=self.config.local_files_only
                    )
                    model = AutoModel.from_pretrained(
                        self.config.model_name, local_files_only=self.config.local_files_only
                    )
                model = model.to(device)
                model.eval()
                self._model, self._tokenizer, self._device = model, tokenizer, device
        return torch, self._model, self._tokenizer, self._device

    def _encode_sync(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        if not texts:
            return []
        torch, model, tokenizer, device = self._ensure_loaded()
        vectors: list[tuple[float, ...]] = []
        with torch.inference_mode(), _autocast_context(torch, device, self.config.use_fp16):
            for start in range(0, len(texts), self.config.batch_size):
                batch = list(texts[start : start + self.config.batch_size])
                encoded = tokenizer(
                    batch,
                    padding=True,
                    truncation=True,
                    max_length=self.config.max_length,
                    return_tensors="pt",
                )
                inputs = {k: v.to(device) for k, v in encoded.items() if hasattr(v, "to")}
                outputs = model(**inputs)
                hidden = outputs.last_hidden_state if hasattr(outputs, "last_hidden_state") else outputs[0]
                if self.pooling == "cls":
                    pooled = hidden[:, 0]
                else:
                    mask = inputs.get("attention_mask")
                    if mask is None:
                        pooled = hidden.mean(dim=1)
                    else:
                        weights = mask.unsqueeze(-1).to(hidden.dtype)
                        denom = weights.sum(dim=1).clamp(min=1e-9)
                        pooled = (hidden * weights).sum(dim=1) / denom
                if self.normalize:
                    pooled = torch.nn.functional.normalize(pooled.float(), p=2, dim=-1)
                else:
                    pooled = pooled.float()
                for row in pooled.detach().cpu().tolist():
                    if not row or not all(math.isfinite(x) for x in row):
                        raise InvalidError("invalid_embeddings", "PyTorch embedder produced non-finite vector values")
                    vectors.append(tuple(float(x) for x in row))
        return vectors

    async def embed(self, identity: Identity, texts: Sequence[str]) -> list[tuple[float, ...]]:
        _ = identity  # Tenant isolation is enforced by Memory/Retrieval vector-store bindings.
        return await asyncio.to_thread(self._encode_sync, list(texts))


class TorchCrossEncoderReranker:
    """Local PyTorch Cross-Encoder implementing `RetrievalEngine`'s `Rerank` hook.

    Scores `(query, candidate)` pairs in mini-batches inside `torch.inference_mode()`
    and maps logits into calibrated `[0, 1]` relevance scores via sigmoid/softmax.
    """

    def __init__(
        self,
        model_name: str,
        *,
        device: str = "auto",
        batch_size: int = 32,
        max_length: int = 512,
        use_fp16: bool = True,
        local_files_only: bool = False,
        loader: Callable[[], tuple[Any, Any]] | None = None,
    ) -> None:
        self.config = TorchModelConfig(
            model_name=model_name,
            device=device,
            batch_size=batch_size,
            max_length=max_length,
            use_fp16=use_fp16,
            local_files_only=local_files_only,
        )
        self.config.validate()
        self._loader = loader
        self._lock = threading.Lock()
        self._model: Any = None
        self._tokenizer: Any = None
        self._device: str | None = None

    def _ensure_loaded(self) -> tuple[Any, Any, Any, str]:
        torch = _require_torch()
        if self._model is not None and self._tokenizer is not None and self._device is not None:
            return torch, self._model, self._tokenizer, self._device
        with self._lock:
            if self._model is None or self._tokenizer is None or self._device is None:
                device = select_device(self.config.device, torch_mod=torch)
                if self._loader is not None:
                    model, tokenizer = self._loader()
                else:
                    AutoTokenizer, _, AutoModelForSequenceClassification = _require_transformers()
                    tokenizer = AutoTokenizer.from_pretrained(
                        self.config.model_name, local_files_only=self.config.local_files_only
                    )
                    model = AutoModelForSequenceClassification.from_pretrained(
                        self.config.model_name, local_files_only=self.config.local_files_only
                    )
                model = model.to(device)
                model.eval()
                self._model, self._tokenizer, self._device = model, tokenizer, device
        return torch, self._model, self._tokenizer, self._device

    def _score_sync(self, query: str, candidates: Sequence[str]) -> list[float]:
        if not candidates:
            return []
        torch, model, tokenizer, device = self._ensure_loaded()
        scores: list[float] = []
        with torch.inference_mode(), _autocast_context(torch, device, self.config.use_fp16):
            for start in range(0, len(candidates), self.config.batch_size):
                batch_passages = list(candidates[start : start + self.config.batch_size])
                batch_queries = [query] * len(batch_passages)
                encoded = tokenizer(
                    batch_queries,
                    batch_passages,
                    padding=True,
                    truncation=True,
                    max_length=self.config.max_length,
                    return_tensors="pt",
                )
                inputs = {k: v.to(device) for k, v in encoded.items() if hasattr(v, "to")}
                outputs = model(**inputs)
                logits = outputs.logits if hasattr(outputs, "logits") else outputs[0]
                logits = logits.float()
                if logits.ndim == 1:
                    probs = torch.sigmoid(logits)
                elif logits.shape[-1] == 1:
                    probs = torch.sigmoid(logits.squeeze(-1))
                else:
                    probs = torch.softmax(logits, dim=-1)[:, 1]
                for score in probs.detach().cpu().tolist():
                    value = float(score)
                    if not math.isfinite(value):
                        raise InvalidError("invalid_rerank_score", "PyTorch reranker produced non-finite score")
                    scores.append(value)
        if len(scores) != len(candidates):
            raise InvalidError("rerank_count_mismatch", "PyTorch reranker returned an invalid score count")
        return scores

    async def __call__(self, identity: Identity, query: str, candidates: Sequence[str]) -> list[float]:
        _ = identity  # Candidates have already passed tenant and ACL authorization.
        return await asyncio.to_thread(self._score_sync, query, list(candidates))


def parse_positive_int(env: Mapping[str, str], key: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = env.get(key, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer between {minimum} and {maximum}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be an integer between {minimum} and {maximum}")
    return value
