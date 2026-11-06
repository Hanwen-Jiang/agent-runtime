"""EV-DE run identity, evidence checks and durable judge spending limits.

Deployment provenance is supplied from the operator's immutable release/Registry
exports; a local source hash must never impersonate a Registry revision.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any



def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy only non-secret reproducibility fields."""
    fields = {"runtime_commit", "coding_agent_commit", "deployment_manifest_sha256", "registry_revisions",
              "memory_revision", "memory_config_sha256", "subject_models", "sub_agents"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("freeze-config must contain exactly: " + ", ".join(sorted(fields)))
    for key, length in (("runtime_commit", 40), ("coding_agent_commit", 40), ("deployment_manifest_sha256", 64), ("memory_config_sha256", 64)):
        if not re.fullmatch(r"[a-f0-9]{" + str(length) + r"}", str(value[key])):
            raise ValueError(f"freeze-config {key} must be a full immutable digest")
    if not isinstance(value["memory_revision"], str) or not value["memory_revision"].strip():
        raise ValueError("freeze-config needs the deployed Memory revision")
    if value["sub_agents"] not in ("off", "rules"):
        raise ValueError("freeze-config sub_agents must be off or rules")
    models = value["subject_models"]
    if not isinstance(models, list) or not models or any(not isinstance(m, str) or not m for m in models):
        raise ValueError("freeze-config needs exact subject_models")
    revisions = value["registry_revisions"]
    if not isinstance(revisions, list) or not revisions:
        raise ValueError("freeze-config needs actual Registry document revisions")
    for row in revisions:
        if not isinstance(row, dict) or set(row) != {"scope", "kind", "name", "version", "document_sha256"}:
            raise ValueError("Registry revision requires scope/kind/name/version/document_sha256")
        if any(not isinstance(row[k], str) or not row[k] for k in row):
            raise ValueError("Registry revision fields must be nonempty strings")
        if not re.fullmatch(r"v[1-9][0-9]*", row["version"]) or not re.fullmatch(r"[a-f0-9]{64}", row["document_sha256"]):
            raise ValueError("Registry evidence needs its published vN and document digest")
        if row["kind"] == "model_profile" and row["scope"] != "platform":
            raise ValueError("Model Profile Registry scope must be platform")
    profiles = [row["name"] for row in revisions if row["kind"] == "model_profile"]
    if len(profiles) != len(set(profiles)) or set(models) - set(profiles):
        raise ValueError("subject models need unique published Registry identities")
    return json.loads(json.dumps(value))


def source_digest() -> str:
    base = Path(__file__).parent
    sources = ["deliverables.py", "deepeval_acceptance.py", "deepeval_ledger.py", "deepeval_metrics.py", "judging.py", "budget.py", "bridges/deepeval_bridge.py",
               "../benchmarks/swebench.py", "../benchmarks/client.py"]
    return digest({name: hashlib.sha256((base / name).read_bytes()).hexdigest() for name in sources})


def identity(cases: Sequence[Any], files: Sequence[Any], metrics: Mapping[str, Any], settings: Any,
             frozen: Mapping[str, Any] | None) -> dict[str, Any]:
    if any(not math.isfinite(v) or v < 0 for v in (settings.budget_usd, settings.task_usd_limit, settings.task_timeout_s)):
        raise ValueError("run limits must be finite and nonnegative")
    return {
        "cases_sha256": digest([{k: getattr(c, k) for k in ("instance_id", "message", "expected_output", "metrics", "confirmed")} for c in cases]),
        "fixture_sha256": digest(files), "rubric_sha256": digest(metrics),
        "collector_scorer_sha256": source_digest(),
        "framework_requirements_sha256": hashlib.sha256((Path(__file__).resolve().parents[3] / "eval-requirements.txt").read_bytes()).hexdigest(),
        "settings": {**settings.pinned(), "budget_usd": settings.budget_usd, "task_usd_limit": settings.task_usd_limit,
                     "concurrency": settings.concurrency, "task_timeout_s": settings.task_timeout_s},
        "deployment": provenance(frozen) if frozen is not None else None,
    }


def evidence_status(record: Mapping[str, Any]) -> str:
    evidence = record.get("tool_evidence")
    if not isinstance(evidence, dict) or not isinstance(evidence.get("events"), list):
        return "missing"
    if evidence.get("truncated"):
        return "truncated"
    if not evidence["events"]:
        return "empty"
    if evidence.get("source") != "task_api_sse" or not record.get("task_id") or evidence.get("task_id") != record["task_id"]:
        return "legacy_unbound"
    return "captured"


def evidence_summary(ids: Sequence[str], records: Mapping[str, Any]) -> dict[str, Any]:
    states = {cid: evidence_status(records.get(cid) or {}) for cid in ids}
    return {"cases": states, "captured": sum(s == "captured" for s in states.values()),
            "all_captured": bool(states) and all(s == "captured" for s in states.values())}



# Keep the EV-DE import stable while Matrix shares the same spending guard.
from agentruntime_learning.evals.budget import BudgetError as JudgeBudgetError, BudgetGateway  # noqa: E402,F401
