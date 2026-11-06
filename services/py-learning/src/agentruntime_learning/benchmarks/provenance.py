"""Frozen experiment identities and auditable, deduplicated historical costs.

Registry versions come from published documents. Git commits identify code only.
Callers must supply observed snapshots; validation is not live attestation.
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
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def validate(value: Mapping[str, Any]) -> dict[str, Any]:
    doc = json.loads(json.dumps(value))
    for key in ("runtime_commit", "gateway_config_sha256", "worker_config_sha256", "tenant_policy_sha256",
                "models", "registry", "memory", "sub_agent_policy", "scoring_version"):
        if not doc.get(key):
            raise ValueError(f"experiment provenance missing {key}")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", doc["runtime_commit"]):
        raise ValueError("runtime_commit must be a full commit")
    for key in ("gateway_config_sha256", "worker_config_sha256", "tenant_policy_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(doc[key])):
            raise ValueError(f"invalid {key}")
    if doc["sub_agent_policy"] not in ("off", "rules"):
        raise ValueError("sub_agent_policy must be off or rules")
    if not isinstance(doc["memory"], dict) or not all(doc["memory"].get(k) for k in ("revision", "corpus_sha256")):
        raise ValueError("memory must freeze revision and corpus_sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", str(doc["memory"]["corpus_sha256"])):
        raise ValueError("invalid memory corpus digest")
    if not isinstance(doc["models"], dict) or not all(doc["models"].get(role) for role in ("plan", "act", "judge", "route", "sub_agent")):
        raise ValueError("models must fix plan, act, judge, route and sub_agent profiles")
    if not isinstance(doc["registry"], dict):
        raise ValueError("registry must contain published profile identities")
    for profile in set(doc["models"].values()):
        identity = doc["registry"].get(profile, {})
        if not all(identity.get(k) for k in ("revision", "scope", "document_sha256")):
            raise ValueError(f"published Registry identity missing for {profile}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(identity["document_sha256"])):
            raise ValueError("invalid Registry document digest")
    return doc


def comparable(left: Mapping[str, Any], right: Mapping[str, Any], *, paired: bool = False) -> bool:
    a, b = validate(left), validate(right)
    if paired:
        if {a.pop("sub_agent_policy"), b.pop("sub_agent_policy")} != {"off", "rules"}:
            return False
    return a == b


def historical_cost(paths: Sequence[Path]) -> dict[str, Any]:
    """Union by task id, so copied/rescored records never double-charge a run.

    A submitted task with no bill is unknown; callers must reconcile it with
    model_usage before deciding the remaining budget. Conflicting bills fail.
    """
    costs: dict[str, float | None] = {}
    for root in paths:
        for path in sorted((Path(root) / "records").glob("*.json")):
            rec = json.loads(path.read_text("utf-8"))
            task = rec.get("task_id")
            if not task:
                continue
            cost = rec.get("cost_usd")
            if cost is not None:
                cost = float(cost)
                if not math.isfinite(cost) or cost < 0:
                    raise ValueError("invalid recorded task cost")
            if task in costs and costs[task] is not None and cost is not None and abs(costs[task] - cost) > 1e-9:
                raise ValueError(f"conflicting cost for task {task}; reconcile model_usage")
            if task not in costs or cost is not None:
                costs[task] = cost
    return {"known_usd": round(sum(v for v in costs.values() if v is not None), 6),
            "unknown_task_ids": sorted(k for k, v in costs.items() if v is None), "tasks": len(costs),
            "source": "recorded_task_usage; final settlement must be checked against model_usage"}
