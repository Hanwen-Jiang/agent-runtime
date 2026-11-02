"""Small, optional Ragas 0.4.3 adapter for the first evaluation slice.

The adapter deliberately keeps the dataset/report contract independent of the
Ragas import.  This lets fixture validation and report generation run in the
base learning environment where Ragas is not installed.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


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
            return float(await value) if hasattr(value, "__await__") else float(value)
        if hasattr(metric, "ascore"):
            value = metric.ascore(sample)
            return float(await value) if hasattr(value, "__await__") else float(value)
    except Exception:
        return None
    return None


async def run_cases(cases: Iterable[RagasCase], *, metrics: Iterable[str] | None = None) -> dict[str, Any]:
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
            else:
                score = await _score(classes[name](), payload)
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


__all__ = ["RagasCase", "load_cases", "run_cases", "run_fixture"]
