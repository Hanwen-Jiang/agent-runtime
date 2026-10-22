"""重复跑评测（TMP 计划 T-02、T-03）：真模型有随机性、样本又少，单次差一两题不下结论。

同一被测对象跑 repeat 次：每次的报告照常落盘（保留单次结果，都和这批开始前的最近一份报告比较），另写一份汇总
repeat--<评测集>--<被测对象>--<划分>--<时间>.json：

    kind            "repeat"
    eval_set, subject, split, created_at, labels
    repeat          次数
    runs            每次报告的路径（不写文件时为 null）
    metrics         {指标: {values, median, min, max}}：报告 metrics 展平后的数值指标，另加 failures.<code>
                    （该失败代码一次评测里出现的次数，没出现记 0）；values 按次序，某次没有该指标记 null，中位数只算有值的
    gate            null，或 gates.evaluate_gate 的结果（按中位数判）

留出集的单次报告本来就只有汇总，汇总里也只有指标，不出现用例。
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentruntime_learning.evals.cases import EvalSet
from agentruntime_learning.evals.gates import GATES, evaluate_gate, gate_lines
from agentruntime_learning.evals.model import Subject
from agentruntime_learning.evals.runner import DEFAULT_REPORTS_DIR, LATEST, EvalConfigError, Report, _flatten, _report_name, latest_report, run_eval

FAILURES = "failures."

# 摘要里给出的指标：(指标, 说明, 比例还是次数或金额)。
_SHOWN = (
    ("success_rate", "成功率", "rate"),
    ("planner.triage_accuracy", "Triage 准确率", "rate"),
    ("planner.schema_pass_rate", "Stage 的 schema 通过率", "rate"),
    ("planner.shape_match_rate", "形状符合率", "rate"),
    ("planner.rule_violations", "规则违例", "count"),
    ("rounds.verdict_accuracy", "判定点 Verdict 准确率", "rate"),
    ("reviewer.verdict_accuracy", "Reviewer 样本 Verdict 准确率", "rate"),
    ("stuck_retry_errors", "该升级却原地重试", "count"),
    ("cost.total_usd", "合计成本", "usd"),
    ("cost.per_success_usd", "每次成功成本", "usd"),
    ("cost.planner_cost_ratio.mean", "规划成本占 Task 预计成本（均值）", "rate"),
    ("cost.planner_cost_ratio.max", "规划成本占 Task 预计成本（最大）", "rate"),
    ("model_call_failures", "模型调用失败", "count"),
)


def _fmt(value: float, kind: str) -> str:
    return {"rate": f"{value:.1%}", "count": f"{value:g}", "usd": f"${value:.4f}"}[kind]


@dataclass
class RepeatedReport:
    runs: list[Report]
    data: dict[str, Any]
    path: Path | None = None

    def summary(self) -> str:
        d = self.data
        lines = [
            f"评测 {d['eval_set']['name']}（版本 {d['eval_set']['version'][:12]}）· 被测对象 {d['subject']} · 划分 {d['split']} · "
            f"重复 {d['repeat']} 次：指标取中位数，括号里是各次的范围"
        ]
        for name, label, kind in _SHOWN:
            entry = d["metrics"].get(name)
            if entry is None or entry["median"] is None:
                lines.append(f"  {label}：无数据")
                continue
            span = f"（{_fmt(entry['min'], kind)}–{_fmt(entry['max'], kind)}）" if entry["min"] != entry["max"] else ""
            lines.append(f"  {label}：中位数 {_fmt(entry['median'], kind)}{span}")
        degraded = [i + 1 for i, v in enumerate((d["metrics"].get("model_call_failures") or {}).get("values", [])) if v]
        if degraded:
            lines.append(f"⚠ 第 {'、'.join(map(str, degraded))} 次评测有模型调用失败：这几次的分数掺了 Planner、Reviewer 的回退，不代表模型的判断")
        if d["gate"]:
            lines += gate_lines(d["gate"])
        lines.append("单次报告：" + ("、".join(p for p in d["runs"] if p) or "没有写文件"))
        return "\n".join(lines)


def _numbers(report: Report) -> dict[str, float]:
    flat = _flatten(report.data["metrics"])
    counts: Counter[str] = Counter()
    for reason in report.data.get("failure_reasons", []):
        counts[reason["code"]] += reason["count"]
    flat.update({f"{FAILURES}{code}": float(count) for code, count in counts.items()})
    return flat


def aggregate(runs: list[Report]) -> dict[str, dict[str, Any]]:
    """每个数值指标的各次取值、中位数与范围。"""
    numbers = [_numbers(r) for r in runs]
    out: dict[str, dict[str, Any]] = {}
    for name in sorted({k for n in numbers for k in n}):
        default = 0.0 if name.startswith(FAILURES) else None
        values = [n.get(name, default) for n in numbers]
        present = [v for v in values if v is not None]
        out[name] = {
            "values": values,
            "median": statistics.median(present) if present else None,
            "min": min(present) if present else None,
            "max": max(present) if present else None,
        }
    return out


async def run_repeated(
    eval_set: EvalSet,
    subject: Subject,
    *,
    split: str = "dev",
    repeat: int = 3,
    reports_dir: Path | None = DEFAULT_REPORTS_DIR,
    compare: Path | str | None = LATEST,
    require_confirmed: bool = False,
    gate: str | None = None,
) -> RepeatedReport:
    """跑 repeat 次并汇总；gate 是 GATES 里的名字（s09、s10）时按中位数判门槛。reports_dir 为 None 时不写文件。"""
    if repeat < 1:
        raise EvalConfigError(f"repeat 至少是 1，得到 {repeat}")
    if gate is not None and gate not in GATES:
        raise EvalConfigError(f"没有门槛 {gate}；可选：{'、'.join(GATES)}")
    baseline: Path | str | None = compare
    if compare == LATEST:
        baseline = latest_report(reports_dir, eval_set=eval_set.name, subject=subject.name, split=split) if reports_dir is not None else None
    runs = [
        await run_eval(eval_set, subject, split=split, reports_dir=reports_dir, compare=baseline, require_confirmed=require_confirmed)
        for _ in range(repeat)
    ]
    metrics = aggregate(runs)
    data: dict[str, Any] = {
        "kind": "repeat",
        "eval_set": runs[0].data["eval_set"],
        "subject": subject.name,
        "split": split,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+0000"),
        "labels": runs[0].data["labels"],
        "repeat": repeat,
        "runs": [str(r.path) if r.path else None for r in runs],
        "metrics": metrics,
        "gate": evaluate_gate(gate, metrics) if gate else None,
    }
    path = None
    if reports_dir is not None:
        reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = data["created_at"].replace("-", "").replace(":", "").replace("+0000", "Z")
        path = reports_dir / f"repeat--{_report_name(eval_set.name, subject.name, split)}{stamp}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    return RepeatedReport(runs=runs, data=data, path=path)
