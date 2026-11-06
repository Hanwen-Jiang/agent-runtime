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

WORKER_NORMALIZATION = "worker-config-exclude-sub-agent-policy-v1"
SUB_AGENT_ENV = "AR_SUB_AGENT_POLICY"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def worker_configuration(configuration: Mapping[str, Any]) -> dict[str, Any]:
    """Hash observed worker specs, removing only explicit sub-agent switches.

    Accept environment maps and Kubernetes ``env`` arrays. Preserve image,
    arguments, queues, every other environment entry and secret *references*.
    Snapshot collectors must not resolve secret references into secret values.
    The observed source plus removal paths make normalization independently
    reproducible; callers cannot simply assert that unrelated hashes match.
    """
    source = json.loads(json.dumps(configuration))
    if not isinstance(source, dict):
        raise ValueError("worker configuration must be an object")
    removed: list[dict[str, str]] = []

    def escape(value: str) -> str:
        return value.replace("~", "~0").replace("/", "~1")

    def remove(value: Any, path: str) -> None:
        if value not in ("off", "rules"):
            raise ValueError("observe the explicit AR_SUB_AGENT_POLICY value before freezing")
        removed.append({"path": path, "value": value})

    def walk(value: Any, path: str = "") -> Any:
        if isinstance(value, dict):
            out = {}
            for key, child in value.items():
                child_path = path + "/" + escape(key)
                if key == SUB_AGENT_ENV:
                    remove(child, child_path)
                elif key == "env" and isinstance(child, list):
                    env = []
                    for index, entry in enumerate(child):
                        item_path = child_path + "/" + str(index)
                        if isinstance(entry, dict) and entry.get("name") == SUB_AGENT_ENV:
                            if set(entry) != {"name", "value"}:
                                raise ValueError("sub-agent environment entry must have an observed literal value")
                            remove(entry["value"], item_path)
                        else:
                            env.append(walk(entry, item_path))
                    out[key] = env
                else:
                    out[key] = walk(child, child_path)
            return out
        if isinstance(value, list):
            return [walk(child, path + "/" + str(i)) for i, child in enumerate(value)]
        return value

    normalized = walk(source)
    if not removed:
        raise ValueError("worker configuration has no observed AR_SUB_AGENT_POLICY switch")
    return {"normalization": WORKER_NORMALIZATION, "configuration": source,
            "raw_sha256": digest(source), "normalized_sha256": digest(normalized), "removed": removed}


def validate(value: Mapping[str, Any]) -> dict[str, Any]:
    doc = json.loads(json.dumps(value))
    for key in ("runtime_commit", "gateway_config_sha256", "worker_config_sha256", "worker_configuration", "tenant_policy_sha256",
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
    observed = doc["worker_configuration"]
    if not isinstance(observed, dict) or "configuration" not in observed:
        raise ValueError("worker normalization needs its observed source configuration")
    recomputed = worker_configuration(observed["configuration"])
    if recomputed != observed or doc["worker_config_sha256"] != recomputed["normalized_sha256"]:
        raise ValueError("worker configuration hashes or normalization evidence do not match")
    if any(change["value"] != doc["sub_agent_policy"] for change in recomputed["removed"]):
        raise ValueError("observed worker sub-agent switches do not match the arm")
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
        if identity["scope"] != "platform" or not re.fullmatch(r"v[1-9][0-9]*", str(identity["revision"])):
            raise ValueError("Registry identity must use platform scope and its real vN published revision")
        if not re.fullmatch(r"[0-9a-f]{64}", str(identity["document_sha256"])):
            raise ValueError("invalid Registry document digest")
    return doc


def comparable(left: Mapping[str, Any], right: Mapping[str, Any], *, paired: bool = False) -> bool:
    a, b = validate(left), validate(right)
    if paired:
        if {a.pop("sub_agent_policy"), b.pop("sub_agent_policy")} != {"off", "rules"}:
            return False
        # Both sources were independently reconstructed and checked above.
        # Only arm-specific raw bytes/removal values differ; normalized hashes
        # stay in the comparison along with every other experimental condition.
        a.pop("worker_configuration")
        b.pop("worker_configuration")
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
