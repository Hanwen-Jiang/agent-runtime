"""Small, isolated Terminal-Bench/Harbor bridge.

The first release deliberately supports an offline fixture/oracle contract.  Harbor is
an optional dependency; importing this module never requires it and a real run fails
with an actionable preflight error when Harbor 0.24.0 is unavailable.
"""
from __future__ import annotations

import importlib
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

HARBOR_VERSION = "0.24.0"
DATASET_VERSION = "terminal-bench-2.0"
HARBOR_AGENT_IMPORT = "agentruntime_learning.benchmarks.terminalbench:RuntimeBridgeAgent"

try:  # Harbor remains optional for fixture/oracle mode.
    from harbor.agents.base import BaseAgent as _HarborBaseAgent  # type: ignore
    from harbor.environments.base import BaseEnvironment as _HarborEnvironment  # type: ignore
    from harbor.models.agent.context import AgentContext as _HarborAgentContext  # type: ignore
except ImportError:  # pragma: no cover - exercised by the base learning environment
    _HarborBaseAgent = object  # type: ignore[assignment,misc]
    _HarborEnvironment = Any  # type: ignore[assignment,misc]
    _HarborAgentContext = Any  # type: ignore[assignment,misc]

# Stable five-task smoke plan. Task ids are intentionally data-only until the
# official dataset is installed; callers may provide a manifest with real ids.
SMOKE_TASKS = [
    {"task_id": f"tbench-2.0-smoke-{i:02d}", "task_name": f"smoke-{i:02d}",
     "verifier": {"status": "passed", "reward": 1.0}}
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


class RuntimeBridgeAgent(_HarborBaseAgent):
    """Harbor agent that delegates the task to a Runtime command in the same container.

    Harbor owns environment creation and verifier execution.  The command is supplied
    by the isolated eval deployment through ``AR_TERMINALBENCH_RUNTIME_COMMAND`` and
    receives the task instruction through ``{instruction}``; its exit code is the
    Runtime-side success signal.  This keeps the bridge explicit without changing the
    production Worker or monkey-patching Harbor internals.
    """

    def __init__(self, *args: Any, runtime_command: str | None = None, **kwargs: Any) -> None:
        if _HarborBaseAgent is object:
            self.logs_dir = kwargs.get("logs_dir")
            self.model_name = kwargs.get("model_name")
        else:
            super().__init__(*args, **kwargs)
        self.runtime_command = runtime_command or os.environ.get("AR_TERMINALBENCH_RUNTIME_COMMAND", "").strip()

    @staticmethod
    def name() -> str:
        return "agentruntime-runtime-bridge"

    def version(self) -> str:
        return "1.0.0"

    async def setup(self, environment: _HarborEnvironment) -> None:
        if not self.runtime_command:
            raise RuntimeError("AR_TERMINALBENCH_RUNTIME_COMMAND is required for the Harbor Runtime bridge")

    async def run(self, instruction: str, environment: _HarborEnvironment, context: _HarborAgentContext) -> None:
        if not self.runtime_command:
            raise RuntimeError("AR_TERMINALBENCH_RUNTIME_COMMAND is required for the Harbor Runtime bridge")
        command = self.runtime_command.format(
            instruction=shlex.quote(instruction),
            workspace="/workspace",
            budget_usd=os.environ.get("AR_TERMINALBENCH_BUDGET_USD", ""),
        )
        result = await environment.exec(command, cwd="/workspace")
        context.metadata = {"agentruntime": {"return_code": result.return_code, "stdout": result.stdout or "", "stderr": result.stderr or ""}}
        if result.return_code != 0:
            raise RuntimeError(f"Runtime bridge exited with code {result.return_code}")


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
        return False, "harbor 0.24.0 is not installed; install the isolated eval venv with: uv pip install -r services/py-learning/eval-requirements.txt"
    version = getattr(mod, "__version__", None)
    if version and version != HARBOR_VERSION:
        return False, f"Harbor {HARBOR_VERSION} required, found {version}"
    return True, None


def _harbor_task_names(manifest: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for task in manifest["tasks"]:
        if not isinstance(task, dict):
            raise ValueError("Terminal-Bench tasks must be objects")
        name = task.get("harbor_task") or task.get("task_name")
        if not isinstance(name, str) or not name or name.startswith("smoke-"):
            raise ValueError("harbor mode requires each task to include an official harbor_task name")
        names.append(name)
    return names


def _parse_harbor_results(out: Path, manifest: dict[str, Any]) -> list[Result]:
    by_name = {str(task.get("harbor_task") or task.get("task_name")): task for task in manifest["tasks"]}
    results: list[Result] = []
    seen: set[str] = set()
    for path in sorted(out.rglob("result.json")):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # Harbor writes one job-level result containing ``trial_results`` and may
        # also leave per-trial result files.  Prefer the embedded trial records so
        # a job file is never mistaken for an extra failed task.
        records = data.get("trial_results") if isinstance(data.get("trial_results"), list) else [data]
        for record in records:
            if not isinstance(record, dict):
                continue
            verifier = record.get("verifier_result") or {}
            rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
            reward = None
            if isinstance(rewards, dict):
                numeric = [float(value) for value in rewards.values() if isinstance(value, (int, float)) and not isinstance(value, bool)]
                reward = numeric[0] if numeric else None
            task_name = str(record.get("task_name") or record.get("task_id") or path.parent.name)
            key = str(record.get("trial_name") or f"{task_name}:{record.get('task_id', '')}")
            if key in seen:
                continue
            seen.add(key)
            source = by_name.get(task_name, {})
            status = "completed" if reward is not None else "infra_failed"
            agent_result = record.get("agent_result") or {}
            cost = record.get("cost_usd")
            if cost is None and isinstance(agent_result, dict):
                cost = agent_result.get("cost_usd")
            results.append(Result(str(source.get("task_id") or task_name), task_name, status, "passed" if reward is not None and reward > 0 else "failed", reward, int(record.get("rounds") or 0), float(cost or 0.0), None if reward is not None else "harbor_result_missing_reward"))
    return results


def _run_harbor(manifest: dict[str, Any], out: Path, *, budget_usd: float) -> list[Result]:
    names = _harbor_task_names(manifest)
    if not os.environ.get("AR_TERMINALBENCH_RUNTIME_COMMAND", "").strip():
        raise RuntimeError("runtime_bridge_command_missing: set AR_TERMINALBENCH_RUNTIME_COMMAND")
    out.mkdir(parents=True, exist_ok=True)
    job_dir = out / "harbor-job"
    command = [sys.executable, "-m", "harbor", "run", "--dataset", f"terminal-bench@{DATASET_VERSION.removeprefix('terminal-bench-')}", "--agent", HARBOR_AGENT_IMPORT, "--n-concurrent", "1", "--n-tasks", str(len(names)), "--jobs-dir", str(job_dir), "--yes"]
    for name in names:
        command.extend(["--include-task-name", name])
    env = os.environ.copy()
    env["AR_TERMINALBENCH_BUDGET_USD"] = str(budget_usd)
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False, env=env)
    (out / "harbor.log").write_text(completed.stdout or "", encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"harbor_run_failed: exit code {completed.returncode}; see {out / 'harbor.log'}")
    results = _parse_harbor_results(job_dir, manifest)
    if not results:
        raise RuntimeError("harbor_result_missing: Harbor finished without parseable trial result.json")
    (out / "terminalbench-report.json").write_text(json.dumps({"dataset_version": DATASET_VERSION, "mode": "harbor", "budget_usd": budget_usd, "results": [asdict(r) for r in results]}, indent=2) + "\n", encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({"eval": "terminalbench", "dataset_version": DATASET_VERSION, "mode": "harbor", "budget_usd": budget_usd, "estimated": False, "measured": True, "sample_count": len(results), "completed": sum(r.status == "completed" for r in results), "infra_failed": sum(r.status == "infra_failed" for r in results)}, indent=2) + "\n", encoding="utf-8")
    return results


def run(manifest: dict[str, Any], out: Path, *, budget_usd: float, mode: str = "fixture") -> list[Result]:
    if budget_usd <= 0:
        raise ValueError("budget-usd must be positive")
    if mode not in {"fixture", "oracle", "harbor"}:
        raise ValueError("mode must be fixture, oracle, or harbor")
    ok, error = preflight(require_harbor=mode == "harbor")
    if not ok:
        raise RuntimeError(f"preflight_failed: {error}")
    if mode == "harbor":
        return _run_harbor(manifest, out, budget_usd=budget_usd)
    results: list[Result] = []
    for task in manifest["tasks"]:
        task_id = task["task_id"]
        name = task.get("task_name", task_id)
        # Fixture/oracle uses the same verifier contract as Harbor: explicit status,
        # reward and rounds; missing verifier output is infrastructure failure.
        verifier = task.get("verifier")
        if not isinstance(verifier, dict) or "status" not in verifier:
            results.append(Result(task_id, name, "infra_failed", "not_run", infra_error_code="verifier_missing"))
            continue
        results.append(Result(task_id, name, "completed", str(verifier["status"]), verifier.get("reward"), int(task.get("rounds", 0))))
    out.mkdir(parents=True, exist_ok=True)
    payload = {"dataset_version": manifest.get("dataset_version", DATASET_VERSION), "mode": mode, "budget_usd": budget_usd, "results": [asdict(r) for r in results]}
    (out / "terminalbench-report.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({
        "eval": "terminalbench",
        "dataset_version": manifest.get("dataset_version", DATASET_VERSION),
        "mode": mode,
        "budget_usd": budget_usd,
        "estimated": mode != "harbor",
        "measured": mode == "harbor",
        "sample_count": len(results),
        "completed": sum(r.status == "completed" for r in results),
        "infra_failed": sum(r.status == "infra_failed" for r in results),
    }, indent=2) + "\n", encoding="utf-8")
    return results

