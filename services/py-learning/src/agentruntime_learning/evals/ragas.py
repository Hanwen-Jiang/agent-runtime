"""Small, optional Ragas 0.4.3 adapter for the first evaluation slice.

The adapter deliberately keeps the dataset/report contract independent of the
Ragas import.  This lets fixture validation and report generation run in the
base learning environment where Ragas is not installed.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable



@dataclass(frozen=True)
class RagasCase:
    case_id: str
    split: str
    user_input: str
    retrieved_contexts: list[str]
    reference: str | None = None
    response: str | None = None
    retrieval_backend: str = "fixture"
    embedding_version: str | None = None
    trace_ref: str | None = None


async def _maybe(value: Any) -> Any:
    return await value if hasattr(value, "__await__") else value


class GatewayRagasLLM:
    """Thin injectable Ragas LLM adapter.

    ``generate`` is either a callable accepting ``(prompt, **kwargs)`` or an
    object exposing that method. The return value is normalized to the shape
    accepted by Ragas' llm wrappers while preserving fake-friendly injection.
    """

    def __init__(self, generate: Callable[..., Any], *, model: str = "judge") -> None:
        self._generate = generate
        self.model = model

    async def generate(self, prompt: Any, n: int = 1, temperature: float = 0.0, **kwargs: Any) -> Any:
        text = prompt.to_string() if hasattr(prompt, "to_string") else str(prompt)
        fn = getattr(self._generate, "generate", self._generate)
        result = await _maybe(fn(text, n=n, temperature=temperature, **kwargs))
        if isinstance(result, str):
            return {"generations": [[{"text": result}]]}
        return result

    async def generate_prompt(self, prompt: Any, **kwargs: Any) -> Any:
        return await self.generate(prompt, **kwargs)

    async def agenerate(self, prompt_str: str, schema: Any = None, **kwargs: Any) -> Any:
        """Ragas 0.4.3 collections API: generate JSON matching ``schema``."""
        result = await self.generate(prompt_str, **kwargs)
        if isinstance(result, str):
            text = result
        elif isinstance(result, dict):
            text = result.get("generations", [[{"text": ""}]])[0][0].get("text", "")
        else:
            text = str(result)
        data = json.loads(text) if isinstance(text, str) else text
        if schema is None:
            return data
        if hasattr(schema, "model_validate"):
            return schema.model_validate(data)
        return data


@dataclass
class _GatewayBudget:
    """Shared per-run spend ledger for Ragas LLM and embedding calls."""

    limit_usd_micros: int
    spent_usd_micros: int = 0

    def remaining(self) -> int:
        return max(0, self.limit_usd_micros - self.spent_usd_micros)

    def charge(self, amount: int) -> None:
        self.spent_usd_micros += max(0, int(amount))


try:
    from ragas.llms import InstructorBaseRagasLLM as _InstructorBaseRagasLLM
except ImportError:  # pragma: no cover - optional dependency in the base image
    class _InstructorBaseRagasLLM:  # type: ignore[no-redef]
        pass


class ModelGatewayRagasLLM(_InstructorBaseRagasLLM):
    """Ragas 0.4.3 structured LLM adapter backed by Model Gateway."""

    def __init__(self, gateway: Any, *, tenant_id: str, tier: str = "strong@medium", max_cost_usd_micros: int = 0, budget: _GatewayBudget | None = None) -> None:
        self.gateway, self.tenant_id, self.tier = gateway, tenant_id, tier
        self.max_cost_usd_micros = max(0, int(max_cost_usd_micros))
        self.cost_usd_micros = 0
        self.budget = budget

    def generate(self, prompt: Any, response_model: Any) -> Any:
        """Synchronous Instructor contract for Ragas callers outside an event loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.agenerate(prompt, response_model))
        raise RuntimeError("ModelGatewayRagasLLM.generate cannot run inside an active event loop; use agenerate")

    async def agenerate(self, prompt_str: str, response_model: Any = None, **_: Any) -> Any:
        from agentruntime.common.v1 import enums_pb2, payload_pb2, tenant_pb2
        from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
        from google.protobuf import json_format, struct_pb2

        if hasattr(prompt_str, "to_string"):
            prompt_str = prompt_str.to_string()
        remaining = self.budget.remaining() if self.budget else (self.max_cost_usd_micros - self.cost_usd_micros if self.max_cost_usd_micros else 0)
        if (self.budget and remaining <= 0) or (self.max_cost_usd_micros and remaining <= 0):
            raise RuntimeError("ragas_budget_exhausted")
        output_schema = response_model.model_json_schema() if hasattr(response_model, "model_json_schema") else response_model
        generation = mg.GenerateParams(messages=[mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=str(prompt_str))])
        if output_schema:
            value = struct_pb2.Value()
            json_format.ParseDict(output_schema, value)
            generation.output_schema.CopyFrom(payload_pb2.JsonPayload(value=value))
        req = mg.GenerateRequest(tenant=tenant_pb2.TenantContext(tenant_id=self.tenant_id),
            call=mg.CallContext(node_id="ragas_judge", role=enums_pb2.ROLE_JUDGE, tier=self.tier,
                                max_cost_usd_micros=remaining), generation=generation)
        try:
            response = await self.gateway.generate(req)
        except Exception as exc:
            billed = int(getattr(exc, "billed_cost_usd_micros", 0) or 0)
            self.cost_usd_micros += billed
            if self.budget:
                self.budget.charge(billed)
            raise
        cost = int(response.usage.cost_usd_micros)
        self.cost_usd_micros += cost
        if self.budget:
            self.budget.charge(cost)
        data = json_format.MessageToDict(response.result)
        if response_model is None:
            return response.text or data
        return response_model.model_validate(data) if hasattr(response_model, "model_validate") else data


class ModelGatewayRagasEmbeddings:
    """Ragas embeddings adapter backed by Model Gateway Embed RPC."""

    def __init__(self, gateway: Any, *, tenant_id: str, embedding_version: str, tier: str = "", max_cost_usd_micros: int = 0, budget: _GatewayBudget | None = None) -> None:
        self.gateway, self.tenant_id, self.embedding_version, self.tier = gateway, tenant_id, embedding_version, tier
        self.max_cost_usd_micros = max(0, int(max_cost_usd_micros))
        self.cost_usd_micros = 0
        self.budget = budget

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        from agentruntime.common.v1 import enums_pb2, tenant_pb2
        from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
        remaining = self.budget.remaining() if self.budget else (self.max_cost_usd_micros - self.cost_usd_micros if self.max_cost_usd_micros else 0)
        if (self.budget and remaining <= 0) or (self.max_cost_usd_micros and remaining <= 0):
            raise RuntimeError("ragas_budget_exhausted")
        req = mg.EmbedRequest(tenant=tenant_pb2.TenantContext(tenant_id=self.tenant_id),
                              call=mg.CallContext(node_id="ragas_embedding", role=enums_pb2.ROLE_JUDGE, tier=self.tier,
                                                  max_cost_usd_micros=remaining),
                              texts=texts, embedding_version=self.embedding_version)
        try:
            response = await self.gateway.embed(req)
        except Exception as exc:
            billed = int(getattr(exc, "billed_cost_usd_micros", 0) or 0)
            self.cost_usd_micros += billed
            if self.budget:
                self.budget.charge(billed)
            raise
        cost = int(response.usage.cost_usd_micros)
        self.cost_usd_micros += cost
        if self.budget:
            self.budget.charge(cost)
        return [[float(x) for x in emb.values] for emb in response.embeddings]

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.embed_documents(texts)

    async def aembed_query(self, text: str) -> list[float]:
        return await self.embed_query(text)


class GatewayRagasEmbeddings:
    """Injectable embedding wrapper compatible with Ragas 0.4.x."""

    def __init__(self, embed: Callable[..., Any], *, model: str = "embedding") -> None:
        self._embed = embed
        self.model = model

    async def embed_query(self, text: str) -> list[float]:
        fn = getattr(self._embed, "embed_query", None)
        if fn is None:
            fn = getattr(self._embed, "embed", self._embed)
        try:
            value = await _maybe(fn(text))
        except TypeError:
            value = await _maybe(fn([text]))
        if isinstance(value, list) and value and isinstance(value[0], (int, float)):
            return [float(x) for x in value]
        return [float(x) for x in value[0]]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        fn = getattr(self._embed, "embed_documents", None)
        if fn is None:
            fn = getattr(self._embed, "embed", self._embed)
        value = await _maybe(fn(texts))
        return [[float(x) for x in row] for row in value]


def load_cases(path: str | Path) -> list[RagasCase]:
    """Load a JSON array (or ``{"cases": [...]}``) of Ragas cases."""
    raw = json.loads(Path(path).read_text("utf-8"))
    rows = raw.get("cases", raw) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError("Ragas input must be a JSON array or an object with cases")
    out: list[RagasCase] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"case {i} must be an object")
        case_id = row.get("case_id")
        if not case_id or not row.get("split") or not isinstance(row.get("retrieved_contexts"), list):
            raise ValueError(f"case {i} requires case_id, split and retrieved_contexts")
        out.append(RagasCase(case_id=str(case_id), split=str(row["split"]), user_input=str(row.get("user_input", "")),
                             retrieved_contexts=[str(x) for x in row["retrieved_contexts"]],
                             reference=None if row.get("reference") is None else str(row["reference"]),
                             response=None if row.get("response") is None else str(row["response"]),
                             retrieval_backend=str(row.get("retrieval_backend", "fixture")),
                             embedding_version=row.get("embedding_version"), trace_ref=row.get("trace_ref")))
    return out


def _metric_classes() -> dict[str, Any]:
    try:
        from ragas.metrics.collections import ContextPrecision, ContextRecall, Faithfulness
    except ImportError:
        try:
            from ragas.metrics import ContextPrecision, ContextRecall, Faithfulness
        except ImportError:
            return {}
    return {"context_precision": ContextPrecision, "context_recall": ContextRecall, "faithfulness": Faithfulness}


def _sample(row: dict[str, Any]) -> Any:
    """Build the 0.4.x sample object, with a legacy dict fallback."""
    try:
        from ragas import SingleTurnSample
    except ImportError:
        try:
            from ragas.dataset_schema import SingleTurnSample
        except ImportError:
            return row
    return SingleTurnSample(**row)


async def _score(metric: Any, row: dict[str, Any]) -> float | None:
    """Best-effort compatibility with Ragas 0.4.x metric APIs."""
    sample = _sample(row)
    try:
        if hasattr(metric, "single_turn_ascore"):
            value = metric.single_turn_ascore(sample)
        elif hasattr(metric, "ascore"):
            parameters = inspect.signature(metric.ascore).parameters
            value = metric.ascore(**{name: row[name] for name in parameters if name in row})
        else:
            return None
        value = await value if hasattr(value, "__await__") else value
        return float(getattr(value, "value", value))
    except Exception:
        return None
    return None


async def run_cases(cases: Iterable[RagasCase], *, metrics: Iterable[str] | None = None, llm: Any = None, embeddings: Any = None) -> dict[str, Any]:
    """Run enabled metrics serially and return a JSON-ready manifest/report."""
    wanted = list(metrics or ("context_precision", "context_recall", "faithfulness"))
    classes = _metric_classes()
    results: list[dict[str, Any]] = []
    for case in cases:
        row: dict[str, Any] = {"case_id": case.case_id, "split": case.split, "scores": {}, "unscored": {}}
        payload = {"user_input": case.user_input, "retrieved_contexts": case.retrieved_contexts,
                   "reference": case.reference, "response": case.response}
        for name in wanted:
            if name == "context_recall" and not case.reference:
                row["unscored"][name] = "missing_reference"
            elif name == "faithfulness" and not case.response:
                row["unscored"][name] = "missing_response"
            elif name not in classes:
                row["unscored"][name] = "ragas_not_installed"
            elif llm is None:
                row["unscored"][name] = "gateway_not_configured"
            else:
                try:
                    metric = classes[name](llm=llm, embeddings=embeddings) if embeddings is not None else classes[name](llm=llm)
                except TypeError:
                    metric = classes[name](llm=llm)
                score = await _score(metric, payload)
                if score is None:
                    row["unscored"][name] = "metric_error"
                else:
                    row["scores"][name] = score
        results.append(row)
    return {"eval": "ragas", "version": "0.4.3", "created_at": datetime.now(UTC).isoformat(),
            "sample_count": len(results), "cases": results}


def run_fixture(path: str | Path, *, out: str | Path | None = None) -> dict[str, Any]:
    report = asyncio.run(run_cases(load_cases(path)))
    report["manifest"] = {"eval": "ragas", "dataset": str(path), "mode": "fixture", "estimated": True,
                          "measured": False, "sample_count": report["sample_count"]}
    if out is not None:
        destination = Path(out); destination.mkdir(parents=True, exist_ok=True)
        (destination / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        (destination / "manifest.json").write_text(json.dumps(report["manifest"], ensure_ascii=False, indent=2), "utf-8")
    return report


def run_gateway(path: str | Path, *, gateway: Any, tenant_id: str, budget_usd: float, embedding_version: str | None = None, out: str | Path | None = None) -> dict[str, Any]:
    """Run Ragas through Model Gateway with a measured budget."""
    if budget_usd <= 0:
        raise ValueError("budget-usd must be positive")
    if not _metric_classes():
        raise RuntimeError("ragas_not_installed: install ragas==0.4.3 in the isolated eval venv")
    budget = max(0, int(round(budget_usd * 1_000_000)))
    ledger = _GatewayBudget(budget)
    llm = ModelGatewayRagasLLM(gateway, tenant_id=tenant_id, max_cost_usd_micros=budget, budget=ledger)
    embeddings = ModelGatewayRagasEmbeddings(gateway, tenant_id=tenant_id, embedding_version=embedding_version, max_cost_usd_micros=budget, budget=ledger) if embedding_version else None
    report = asyncio.run(run_cases(load_cases(path), llm=llm, embeddings=embeddings))
    report["manifest"] = {"eval": "ragas", "dataset": str(path), "mode": "gateway", "estimated": False,
                          "measured": True, "budget_usd": budget_usd, "sample_count": report["sample_count"],
                          "cost_usd": ledger.spent_usd_micros / 1_000_000}
    if out is not None:
        destination = Path(out); destination.mkdir(parents=True, exist_ok=True)
        (destination / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        (destination / "manifest.json").write_text(json.dumps(report["manifest"], ensure_ascii=False, indent=2), "utf-8")
    return report


__all__ = ["GatewayRagasEmbeddings", "GatewayRagasLLM", "ModelGatewayRagasEmbeddings", "ModelGatewayRagasLLM", "RagasCase", "load_cases", "run_cases", "run_fixture", "run_gateway"]
