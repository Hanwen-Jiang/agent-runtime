"""观测式评测与非劣效门槛（附录 J.3；T-16 第 4 步；与影子评测的出入见 docs/plan/learning-pipeline.md 第 4 节第 1 条）。

- 候选组：签名等于候选签名的全部 Task（成功与失败都算），加上签名是它的严格前缀、且没有成功的 Task（走这条路中途失败的）。
  只算成功的会有选择偏差。
- 现行组：该任务族当前的默认 Recipe 有就取用它的 Task；没有就取该任务族全部完整规划的 Task。
- 门槛：候选成功率的 Wilson 95% 下界 ≥ 现行成功率 − δ；候选每次成功成本的 95% 上界（均值 + 1.96 × 标准误）
  ≤ 现行均值 × (1 + ε)；两组都至少 min_samples 条。默认 δ = 0.02、ε = 0.05、100 条（J.3）。
- 每次成功成本是成功 Task 成本的均值。
"""

from __future__ import annotations

import hashlib
import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from agentruntime_learning.pipeline.candidates import Signature, signature
from agentruntime_learning.pipeline.traces import TaskTrace

Z95 = 1.96
DEFAULT_DELTA = 0.02
DEFAULT_EPSILON = 0.05
DEFAULT_MIN_SAMPLES = 100


def wilson(successes: int, total: int, z: float = Z95) -> tuple[float, float]:
    """成功率的 Wilson 置信区间。"""
    if total <= 0:
        raise ValueError("没有样本，算不出置信区间")
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    low = 0.0 if successes == 0 else max(0.0, center - half)
    high = 1.0 if successes == total else min(1.0, center + half)
    return low, high


@dataclass(frozen=True)
class Outcomes:
    total: int
    successes: int
    success_costs: tuple[float, ...] = ()

    @classmethod
    def from_tasks(cls, tasks: Iterable[TaskTrace]) -> Outcomes:
        tasks = list(tasks)
        costs = tuple(t.cost_usd for t in tasks if t.status == "done")
        return cls(total=len(tasks), successes=len(costs), success_costs=costs)

    @property
    def success_rate(self) -> float:
        return self.successes / self.total if self.total else 0.0

    @property
    def cost_per_success(self) -> float | None:
        return statistics.fmean(self.success_costs) if self.success_costs else None

    def cost_upper_bound(self) -> float | None:
        """每次成功成本的 95% 上界：均值 + 1.96 × 标准误（一条样本时标准误按 0）。"""
        if not self.success_costs:
            return None
        n = len(self.success_costs)
        sd = statistics.stdev(self.success_costs) if n > 1 else 0.0
        return statistics.fmean(self.success_costs) + Z95 * sd / math.sqrt(n)

    def result(self) -> dict[str, Any]:
        """registry_evaluation.schema.json 的 result。"""
        out: dict[str, Any] = {"success_rate": round(self.success_rate, 6), "cost_per_success_usd": round(self.cost_per_success or 0.0, 6)}
        if self.total:
            low, high = wilson(self.successes, self.total)
            out["success_rate_ci95"] = [round(low, 6), round(high, 6)]
        return out


def candidate_cohort(tasks: Iterable[TaskTrace], task_family: str, sig: Signature) -> list[TaskTrace]:
    out = []
    for t in tasks:
        if t.task_family != task_family or t.recipe:
            continue
        s = signature(t)
        if s is None:
            continue
        if s == sig or (t.status != "done" and len(s) < len(sig) and tuple(sig[: len(s)]) == s):
            out.append(t)
    return sorted(out, key=lambda t: t.task_id)


def baseline_cohort(tasks: Iterable[TaskTrace], task_family: str, recipe: str | None) -> list[TaskTrace]:
    """recipe 为 None：该任务族全部完整规划的 Task；否则是用了这个 Recipe（name@vN）的 Task。"""
    if recipe is None:
        out = [t for t in tasks if t.task_family == task_family and not t.recipe]
    else:
        out = [t for t in tasks if t.task_family == task_family and t.recipe == recipe]
    return sorted(out, key=lambda t: t.task_id)


@dataclass
class GateResult:
    passed: bool
    reasons: list[str]
    candidate: dict[str, Any]
    baseline: dict[str, Any] | None
    thresholds: dict[str, float]
    min_samples: int
    samples: int
    details: dict[str, Any] = field(default_factory=dict)


def non_inferiority(
    candidate: Outcomes,
    baseline: Outcomes,
    *,
    delta: float = DEFAULT_DELTA,
    epsilon: float = DEFAULT_EPSILON,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> GateResult:
    reasons: list[str] = []
    details: dict[str, Any] = {}
    if candidate.total < min_samples:
        reasons.append(f"样本不足：候选组 {candidate.total} 条，至少要 {min_samples} 条")
    if baseline.total == 0:
        reasons.append("现行做法没有 trace，无从比较")
    elif baseline.total < min_samples:
        reasons.append(f"样本不足：现行组 {baseline.total} 条，至少要 {min_samples} 条")
    if candidate.total and baseline.total:
        low = wilson(candidate.successes, candidate.total)[0]
        floor = baseline.success_rate - delta
        details["success_rate"] = {"candidate_low": round(low, 6), "baseline": round(baseline.success_rate, 6), "floor": round(floor, 6)}
        if low < floor:
            reasons.append(f"成功率的 95% 置信下界 {low:.1%} 低于现行的成功率 {baseline.success_rate:.1%} 减 δ（{delta:.0%}）")
    upper, base_cost = candidate.cost_upper_bound(), baseline.cost_per_success
    if upper is None:
        reasons.append("候选组没有成功的 Task")
    elif base_cost is not None:
        ceiling = base_cost * (1 + epsilon)
        details["cost_per_success"] = {"candidate_upper": round(upper, 6), "baseline": round(base_cost, 6), "ceiling": round(ceiling, 6)}
        if upper > ceiling:
            reasons.append(f"每次成功成本的 95% 上界 ${upper:.4f} 高于现行的 ${base_cost:.4f} × (1 + ε)（ε = {epsilon:.0%}）")
    return GateResult(
        passed=not reasons,
        reasons=reasons,
        candidate=candidate.result(),
        baseline=baseline.result() if baseline.total else None,
        thresholds={"delta": delta, "epsilon": epsilon},
        min_samples=min_samples,
        samples=candidate.total,
        details=details,
    )


def evidence(gate: GateResult, *, task_family: str, task_ids: Sequence[str], report: str, evaluated_at: str) -> dict[str, Any]:
    """registry_evaluation.schema.json 的评测证据。评测集的版本是参与评测的 task_id 集合的哈希。"""
    version = hashlib.sha256("\n".join(sorted(set(task_ids))).encode()).hexdigest()[:16]
    doc: dict[str, Any] = {
        "eval_set": {"name": f"traces.{task_family}", "version": version},
        "task_family": task_family,
        "samples": max(gate.samples, 1),
        "candidate": gate.candidate,
        "thresholds": dict(gate.thresholds),
        "passed": gate.passed,
        "report": report,
        "evaluated_at": evaluated_at,
    }
    if gate.baseline is not None:
        doc["baseline"] = gate.baseline
    return doc
