"""S-09、S-10 的门槛（计划 S-09、S-10 的验收；TMP 计划 T-03）：按重复跑的中位数判定。

指标名是报告 metrics 展平后的名字（例如 planner.triage_accuracy），另有 failures.<code>：该失败代码在一次评测里出现的次数。
没有数据的指标算未达标（例如没有带预计成本的 full 用例时，规划成本占比算不出来），failures.<code> 没出现按 0 算。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Check:
    metric: str
    op: str
    """>= 或 <=。"""
    threshold: float
    label: str
    kind: str = "rate"
    """rate（比例，差距按百分点）或 count（次数）。"""
    missing_is_zero: bool = False


GATES: dict[str, tuple[Check, ...]] = {
    "s09": (
        Check("planner.triage_accuracy", ">=", 0.9, "Triage 准确率"),
        Check("planner.schema_pass_rate", ">=", 1.0, "Stage 的 schema 通过率"),
        Check("planner.rule_violations", "<=", 0, "规则违例", kind="count"),
        Check("failures.keep_nodes", "<=", 0, "replan 重做已验证的 Node", kind="count", missing_is_zero=True),
        # 设计书 3.2.2：full 的成本不超过该 Task 预计成本的约 5%；按每个 Task 算，取最大的一条。
        Check("cost.planner_cost_ratio.max", "<=", 0.05, "full 的规划成本占 Task 预计成本（最大的一条）"),
    ),
    "s10": (
        Check("reviewer.verdict_accuracy", ">=", 0.85, "Reviewer 样本的 Verdict 准确率"),
        Check("stuck_retry_errors", "<=", 0, "该升级却原地重试", kind="count"),
        Check("reviewer.schema_pass_rate", ">=", 1.0, "Reviewer 样本的 schema 通过率"),
        Check("rounds.schema_pass_rate", ">=", 1.0, "判定点的 schema 通过率"),
    ),
}


def _show(check: Check, value: float) -> str:
    return f"{value:.1%}" if check.kind == "rate" else f"{value:g} 次"


def _gap(check: Check, median: float | None) -> str:
    if median is None:
        return "无数据：这个指标在评测里算不出来"
    target = f"门槛 {'≥' if check.op == '>=' else '≤'} {_show(check, check.threshold)}"
    if check.kind == "count":
        return f"中位数 {_show(check, median)}，{target}"
    points = abs(median - check.threshold) * 100
    return f"中位数 {_show(check, median)}，{target}，{'差' if check.op == '>=' else '超出'} {points:.1f} 个百分点"


def evaluate_gate(name: str, metrics: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """metrics 是汇总里的 {指标: {values, median, min, max}}。返回 {name, passed, checks: [...]}，未达标的检查带 gap。"""
    if name not in GATES:
        raise ValueError(f"没有门槛 {name}；可选：{'、'.join(GATES)}")
    checks = []
    for check in GATES[name]:
        entry = metrics.get(check.metric)
        if entry is None and check.missing_is_zero:
            entry = {"median": 0, "min": 0, "max": 0}
        median = entry.get("median") if entry else None
        if median is None:
            passed = False
        elif check.op == ">=":
            passed = median >= check.threshold
        else:
            passed = median <= check.threshold
        checks.append(
            {
                "metric": check.metric,
                "label": check.label,
                "op": check.op,
                "threshold": check.threshold,
                "median": median,
                "min": entry.get("min") if entry else None,
                "max": entry.get("max") if entry else None,
                "passed": passed,
                "gap": "" if passed else _gap(check, median),
            }
        )
    return {"name": name, "passed": all(c["passed"] for c in checks), "checks": checks}


def gate_lines(gate: Mapping[str, Any]) -> list[str]:
    """给人看的门槛结果：每项一行，未达标的写出差距。"""
    lines = [f"门槛 {gate['name']}：" + ("达标" if gate["passed"] else "未达标")]
    for c in gate["checks"]:
        check = next(k for k in GATES[gate["name"]] if k.metric == c["metric"])
        if c["median"] is None:
            value = "无数据"
        else:
            value = _show(check, c["median"])
            if c["min"] is not None and c["max"] is not None and c["min"] != c["max"]:
                value += f"（{_show(check, c['min'])}–{_show(check, c['max'])}）"
        lines.append(f"  {'✓' if c['passed'] else '✗'} {c['label']}：{value}" + (f"；{c['gap']}" if c["gap"] else ""))
    return lines
