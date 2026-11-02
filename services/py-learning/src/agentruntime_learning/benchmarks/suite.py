"""Run independent benchmark adapters with bounded concurrency.

The suite is deliberately a small process coordinator.  Each adapter owns its
dataset, resume semantics, and cost accounting; this module only validates the
plan, gives each child an output directory and token, and records the outcome.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


KNOWN = ("swebench", "terminalbench", "deepeval", "ragas")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ResumeMismatch(ValueError):
    """The output directory belongs to a different benchmark set."""


@dataclass(frozen=True)
class BenchmarkSpec:
    name: str
    budget_usd: float
    args: tuple[str, ...] = ()
    token_env: str = "AR_TASK_API_TOKEN"


@dataclass(frozen=True)
class SuitePlan:
    budget_usd: float
    max_parallel: int
    benchmarks: tuple[BenchmarkSpec, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.benchmarks)


@dataclass(frozen=True)
class BenchmarkResult:
    name: str
    status: str
    exit_code: int | None = None
    duration_s: float = 0.0
    message: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 3),
            **({"message": self.message} if self.message else {}),
        }


@dataclass(frozen=True)
class SuiteSummary:
    status: str
    benchmarks: tuple[BenchmarkResult, ...]


CommandFactory = Callable[[BenchmarkSpec, Path], Sequence[str]]


def _number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} 要是数字")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 要是数字") from None
    if result != result or result in (float("inf"), float("-inf")):
        raise ValueError(f"{field} 要是有限数字")
    return result


def parse_plan(raw: Mapping[str, object]) -> SuitePlan:
    """Validate a JSON plan without touching the filesystem or network."""
    budget = _number(raw.get("budget_usd"), "预算")
    if budget <= 0:
        raise ValueError("预算必须大于 0")
    try:
        max_parallel = int(raw.get("max_parallel", 1))
    except (TypeError, ValueError):
        raise ValueError("max_parallel 要是整数") from None
    if max_parallel < 1:
        raise ValueError("max_parallel 必须至少为 1")
    raw_specs = raw.get("benchmarks")
    if not isinstance(raw_specs, list) or not raw_specs:
        raise ValueError("评测列表不能为空")

    specs: list[BenchmarkSpec] = []
    seen: set[str] = set()
    total = 0.0
    for raw_spec in raw_specs:
        if not isinstance(raw_spec, Mapping):
            raise ValueError("评测项要是对象")
        name = str(raw_spec.get("name") or "")
        if name not in KNOWN:
            raise ValueError(f"不认识的评测 {name or '<empty>'}")
        if name in seen:
            raise ValueError(f"评测名重复：{name}")
        seen.add(name)
        share = _number(raw_spec.get("budget_usd"), f"{name} 的预算")
        if share <= 0:
            raise ValueError(f"{name} 的预算必须大于 0")
        args_value = raw_spec.get("args", [])
        if not isinstance(args_value, list) or not all(isinstance(arg, str) for arg in args_value):
            raise ValueError(f"{name} 的 args 要是字符串列表")
        token_env = str(raw_spec.get("token_env") or "AR_TASK_API_TOKEN")
        if not _ENV_NAME.fullmatch(token_env):
            raise ValueError(f"{name} 的 token_env 不合法")
        specs.append(BenchmarkSpec(name, share, tuple(args_value), token_env))
        total += share
    if total > budget + 1e-9:
        raise ValueError(f"评测预算合计 ${total:.6g} 超过总预算 ${budget:.6g}")
    return SuitePlan(budget, max_parallel, tuple(specs))


def default_command(spec: BenchmarkSpec, out_dir: Path) -> list[str]:
    """Build the child command, keeping adapter-specific arguments opaque."""
    return [
        sys.executable,
        "-m",
        "agentruntime_learning.benchmarks",
        spec.name,
        *spec.args,
        "--out",
        str(out_dir),
        "--budget-usd",
        str(float(spec.budget_usd)),
    ]


def _redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _load_existing(out_dir: Path, plan: SuitePlan) -> dict[str, dict[str, object]]:
    path = out_dir / "suite.json"
    if not path.exists():
        return {}
    try:
        record = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ResumeMismatch(f"读不了已有 suite.json：{exc}") from None
    previous = tuple(item.get("name") for item in record.get("benchmarks", []) if isinstance(item, Mapping))
    if previous != plan.names:
        raise ResumeMismatch("同一个输出目录的评测名单不能改变")
    return {str(item["name"]): dict(item) for item in record.get("benchmarks", []) if isinstance(item, Mapping) and item.get("name")}


def _write_report(out_dir: Path, summary: SuiteSummary, plan: SuitePlan) -> None:
    record = {
        "status": summary.status,
        "budget_usd": plan.budget_usd,
        "max_parallel": plan.max_parallel,
        "benchmarks": [result.as_dict() for result in summary.benchmarks],
    }
    (out_dir / "suite.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Benchmark suite", "", f"status: `{summary.status}`", "", "| benchmark | status | exit | duration (s) |", "| --- | --- | ---: | ---: |"]
    for result in summary.benchmarks:
        lines.append(f"| {result.name} | {result.status} | {result.exit_code if result.exit_code is not None else ''} | {result.duration_s:.3f} |")
        if result.message:
            lines.append(f"  - {result.message}")
    (out_dir / "suite-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_one(spec: BenchmarkSpec, out_dir: Path, env: Mapping[str, str], command: CommandFactory) -> BenchmarkResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    token = env.get(spec.token_env, "")
    # The default variable is intentionally left to the adapter.  This keeps
    # dry/unit-test commands usable without credentials while real adapters
    # still perform their own required-token check.  A custom token variable is
    # a suite-level routing contract, so fail before starting that child.
    if not token and spec.token_env != "AR_TASK_API_TOKEN":
        message = f"缺少令牌环境变量 {spec.token_env}"
        (out_dir.parent / f"{spec.name}.log").write_text(message + "\n", encoding="utf-8")
        return BenchmarkResult(spec.name, "failed", None, 0.0, message)
    child_env = dict(env)
    if token:
        child_env["AR_TASK_API_TOKEN"] = token
    argv = [str(item) for item in command(spec, out_dir)]
    started = time.monotonic()
    try:
        completed = subprocess.run(argv, env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        output = _redact(completed.stdout or "", (token,))
        status = "passed" if completed.returncode == 0 else "failed"
        message = "" if status == "passed" else f"子进程退出码 {completed.returncode}"
        result = BenchmarkResult(spec.name, status, completed.returncode, time.monotonic() - started, message)
    except OSError as exc:
        output = _redact(str(exc), (token,))
        result = BenchmarkResult(spec.name, "failed", None, time.monotonic() - started, output)
    (out_dir.parent / f"{spec.name}.log").write_text(output + ("\n" if output and not output.endswith("\n") else ""), encoding="utf-8")
    return result


def run_suite(
    plan: SuitePlan,
    out_dir: Path,
    *,
    env: Mapping[str, str] | None = None,
    command: CommandFactory = default_command,
) -> SuiteSummary:
    """Run a plan, resuming only failed or incomplete children."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ if env is None else env)
    existing = _load_existing(out_dir, plan)
    results: dict[str, BenchmarkResult] = {}
    pending: list[BenchmarkSpec] = []
    for spec in plan.benchmarks:
        old = existing.get(spec.name)
        if old and old.get("status") == "passed":
            results[spec.name] = BenchmarkResult(
                spec.name,
                "passed",
                int(old["exit_code"]) if old.get("exit_code") is not None else 0,
                float(old.get("duration_s") or 0),
                str(old.get("message") or ""),
            )
        else:
            pending.append(spec)

    with ThreadPoolExecutor(max_workers=plan.max_parallel) as pool:
        futures: dict[Future[BenchmarkResult], str] = {
            pool.submit(_run_one, spec, out_dir / spec.name, environment, command): spec.name for spec in pending
        }
        for future in as_completed(futures):
            result = future.result()
            results[result.name] = result

    ordered = tuple(results[spec.name] for spec in plan.benchmarks)
    status = "passed" if all(result.status == "passed" for result in ordered) else "failed"
    summary = SuiteSummary(status, ordered)
    _write_report(out_dir, summary, plan)
    return summary


def dry_run(plan: SuitePlan, out_dir: Path, command: CommandFactory = default_command) -> list[list[str]]:
    return [[str(part) for part in command(spec, Path(out_dir) / spec.name)] for spec in plan.benchmarks]
