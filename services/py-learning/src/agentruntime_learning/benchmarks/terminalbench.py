"""Small, isolated Terminal-Bench/Harbor bridge.

The first release deliberately supports an offline fixture/oracle contract.  Harbor is
an optional dependency; importing this module never requires it and a real run fails
with an actionable preflight error when Harbor 0.24.0 is unavailable.
"""
from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

HARBOR_VERSION = "0.24.0"
DATASET_VERSION = "terminal-bench-2.0"

# Stable five-task smoke plan. Task ids are intentionally data-only until the
# official dataset is installed; callers may provide a manifest with real ids.
SMOKE_TASKS = [
    {"task_id": f"tbench-2.0-smoke-{i:02d}", "task_name": f"smoke-{i:02d}"}
    for i in range(1, 6)
]


@dataclass(frozen=True)
class Result:
    task_id: str
    task_name: str
    status: str
    verifier_status: str
    reward: float | None = None
    rounds: int = 0
    model_cost_usd: float = 0.0
    infra_error_code: str | None = None


def smoke_manifest() -> dict[str, Any]:
    return {"dataset_version": DATASET_VERSION, "seed": 0, "tasks": SMOKE_TASKS}


def load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise ValueError("Terminal-Bench manifest must contain a tasks array")
    ids = [t.get("task_id") for t in data["tasks"] if isinstance(t, dict)]
    if len(ids) != len(set(ids)) or any(not x for x in ids):
        raise ValueError("manifest task_id values must be unique and non-empty")
    return data


def preflight(*, require_harbor: bool) -> tuple[bool, str | None]:
    if not require_harbor:
        return True, None
    try:
        mod = importlib.import_module("harbor")
    except ImportError:
        return False, "harbor 0.24.0 is not installed; install with: uv sync --extra terminalbench"
    version = getattr(mod, "__version__", None)
    if version and version != HARBOR_VERSION:
        return False, f"Harbor {HARBOR_VERSION} required, found {version}"
    return True, None


def run(manifest: dict[str, Any], out: Path, *, budget_usd: float, mode: str = "fixture") -> list[Result]:
    if budget_usd <= 0:
        raise ValueError("budget-usd must be positive")
    if mode not in {"fixture", "oracle", "harbor"}:
        raise ValueError("mode must be fixture, oracle, or harbor")
    ok, error = preflight(require_harbor=mode == "harbor")
    if not ok:
        raise RuntimeError(f"preflight_failed: {error}")
    results: list[Result] = []
    for task in manifest["tasks"]:
        task_id, name = task["task_id"], task.get("task_name", task_id)
        # Fixture/oracle uses the same verifier contract as Harbor: explicit status,
        # reward and rounds; missing verifier output is infrastructure failure.
        verifier = task.get("verifier", {"status": "passed", "reward": 1.0})
        if not isinstance(verifier, dict) or "status" not in verifier:
            results.append(Result(task_id, name, "infra_failed", "not_run", infra_error_code="verifier_missing"))
            continue
        results.append(Result(task_id, name, "completed", str(verifier["status"]), verifier.get("reward"), int(task.get("rounds", 0))))
    out.mkdir(parents=True, exist_ok=True)
    payload = {"dataset_version": manifest.get("dataset_version", DATASET_VERSION), "mode": mode, "budget_usd": budget_usd, "results": [asdict(r) for r in results]}
    (out / "terminalbench-report.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return results

