"""运行器（计划 S-08）：跑一遍评测集，输出成功率、成本，以及与上一版的差异。

报告（Report.data，写成 JSON）的结构：

    eval_set        {name, version}；version 不同的两份报告不可比，diff 里会注明
    subject, split, created_at
    labels          {confirmed, draft}：标注的人工确认情况
    metrics
      success_rate  Task 用例的成功率：Planner 的决策与每个 Round 的判定点都通过
      planner       {decisions, passed, schema_pass_rate, triage_accuracy, shape_match_rate, rule_violations}
      rounds        Task 用例里的判定点 {decisions, passed, verdict_accuracy, schema_pass_rate}
      reviewer      Reviewer 专用样本 {decisions, passed, verdict_accuracy, schema_pass_rate}
      stuck_retry_errors  “该升级却原地重试”的次数（判定点与样本合计）
      rubric        {scored, mean}；没配 judge 时 scored = 0、mean = null（未评，不按 0 分算）
      cost          {total_usd（被测对象的全部成本）, task_cases_usd（Task 用例的规划与判定）,
                     per_case_usd, per_success_usd（按 Task 用例算）, judge_usd（judge 另计）,
                     planner_cost_ratio: {cases, mean, max}}：标为 full 的用例里，规划成本占 Task 预计成本的比例
    breakdown       {by_category: {类别: {cases, successes, success_rate, cost_usd, per_success_usd}},
                     by_triage: {级别: {cases, triage_correct, successes, success_rate}}}
    failure_reasons [{dimension, code, message, count}]，按次数从多到少：低分的解释
    cases           [{id, kind, passed, failures, cost_usd}]，按 id 排序；留出集的报告没有这一项
                    被测对象调用出错时，该次决策记 error 维度的失败（code = subject_error），整轮评测继续
    diff            null，或 {against, comparable, metrics: {名: {before, after, delta}}, now_failing, now_passing}；
                    留出集的 diff 没有 now_failing 与 now_passing

留出集只给汇总：报告与摘要里不出现逐条结果，避免照着留出集调提示。
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from agentruntime_learning.evals.cases import Checkpoint, EvalSet, TaskCase
from agentruntime_learning.evals.model import PlannerInput, ReviewerInput, Subject
from agentruntime_learning.evals.rubric import RubricJudge
from agentruntime_learning.evals.scoring import DecisionScore, Failure, score_planner, score_reviewer

# 报告默认写在这里（不提交）。
DEFAULT_REPORTS_DIR = Path(__file__).resolve().parents[3] / ".eval-reports"

LATEST: Final = "latest"
"""compare 的默认值：和 reports_dir 里同一被测对象、同一划分、同一评测集的最近一份报告比较。"""

SPLITS = ("dev", "holdout")
# 摘要里列出的失败原因条数。
_TOP_REASONS = 12


class EvalConfigError(ValueError):
    """运行参数不合法，例如 judge 与被测对象同一家，或要求标注全部确认而还有草稿。"""


class UnconfirmedLabelsError(EvalConfigError):
    """要求标注全部经人确认，而还有草稿。"""


@dataclass
class _Decision:
    label: str
    score: DecisionScore


@dataclass
class _ItemResult:
    id: str
    kind: str
    category: str | None
    decisions: list[_Decision] = field(default_factory=list)
    rubric: float | None = None
    rubric_cost: float = 0.0

    @property
    def passed(self) -> bool:
        return all(d.score.passed for d in self.decisions)

    @property
    def cost(self) -> float:
        return sum(d.score.cost_usd for d in self.decisions)


@dataclass
class Report:
    data: dict[str, Any]
    path: Path | None = None

    def summary(self) -> str:
        """给人看的摘要：成功率、成本、主要失败原因、分类明细、与上一版的差异。"""
        d, m = self.data, self.data["metrics"]
        lines = [
            f"评测 {d['eval_set']['name']}（版本 {d['eval_set']['version'][:12]}）· 被测对象 {d['subject']} · 划分 {d['split']}",
            f"标注：已确认 {d['labels']['confirmed']}，待确认 {d['labels']['draft']}"
            + ("（S-09、S-10 的门槛要等标注全部确认，用 --require-confirmed）" if d["labels"]["draft"] else ""),
            f"成功率：{_pct(m['success_rate'])}（{m['planner']['decisions']} 条 Task 用例）",
            f"Planner：Triage 准确率 {_pct(m['planner']['triage_accuracy'])}，形状符合率 {_pct(m['planner']['shape_match_rate'])}，"
            f"结构校验通过率 {_pct(m['planner']['schema_pass_rate'])}，规则违例 {m['planner']['rule_violations']} 次",
            f"判定点：Verdict 准确率 {_pct(m['rounds']['verdict_accuracy'])}（{m['rounds']['decisions']} 个）；"
            f"Reviewer 样本：Verdict 准确率 {_pct(m['reviewer']['verdict_accuracy'])}（{m['reviewer']['decisions']} 条）；"
            f"该升级却原地重试 {m['stuck_retry_errors']} 次",
            "rubric：" + ("未评（没有配 judge）" if not m["rubric"]["scored"] else f"均分 {m['rubric']['mean']:.2f}（{m['rubric']['scored']} 条）"),
        ]
        c = m["cost"]
        ratio = c["planner_cost_ratio"]
        lines.append(
            f"成本：合计 {_usd(c['total_usd'])}（Task 用例 {_usd(c['task_cases_usd'])}，每条 {_usd(c['per_case_usd'])}，每次成功 {_usd(c['per_success_usd'])}）；"
            f"judge 另计 {_usd(c['judge_usd'])}；full 的规划成本占 Task 预计成本："
            + (f"均值 {_pct(ratio['mean'])}，最大 {_pct(ratio['max'])}（{ratio['cases']} 条）" if ratio["cases"] else "无数据")
        )
        reasons = d["failure_reasons"]
        lines.append("主要失败原因：" + ("无" if not reasons else ""))
        lines += [f"  [{r['dimension']}] {r['message']} × {r['count']}" for r in reasons[:_TOP_REASONS]]
        if len(reasons) > _TOP_REASONS:
            lines.append(f"  ……另有 {len(reasons) - _TOP_REASONS} 种")
        lines.append("按类别：")
        for name, b in d["breakdown"]["by_category"].items():
            lines.append(f"  {name}：成功 {b['successes']}/{b['cases']}（{_pct(b['success_rate'])}），成本 {_usd(b['cost_usd'])}，每次成功 {_usd(b['per_success_usd'])}")
        lines.append("按 Triage（标注）：")
        for name, b in d["breakdown"]["by_triage"].items():
            lines.append(f"  {name}：Triage 对 {b['triage_correct']}/{b['cases']}，成功 {b['successes']}/{b['cases']}")
        diff = d.get("diff")
        if diff:
            lines.append(f"与上一版（{diff['against']}）的差异：" + ("" if diff["comparable"] else "评测集版本不同，仅供参考"))
            changed = [(k, v) for k, v in diff["metrics"].items() if v["delta"] not in (None, 0)]
            lines += [f"  {k}：{_num(v['before'])} → {_num(v['after'])}（{v['delta']:+.4g}）" for k, v in changed] or ["  指标没有变化"]
            if "now_failing" in diff:
                lines.append(f"  新失败：{'、'.join(diff['now_failing']) or '无'}；新通过：{'、'.join(diff['now_passing']) or '无'}")
        return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _usd(value: float | None) -> str:
    return "—" if value is None else f"${value:.4f}"


def _num(value: Any) -> str:
    return "—" if value is None else f"{value:.4g}"


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _error(exc: Exception, split: str) -> DecisionScore:
    detail = f"：{exc}" if split == "dev" else ""
    return DecisionScore(passed=False, failures=[Failure("error", "subject_error", f"被测对象出错：{type(exc).__name__}{detail}")])


async def _plan(subject: Subject, case: TaskCase, eval_set: EvalSet, split: str) -> tuple[DecisionScore, Any]:
    inp = case.planner_input
    planner_input = PlannerInput(
        message=inp["message"],
        task_state=copy.deepcopy(inp.get("task_state")),
        attachments=list(inp.get("attachments", [])),
        context=copy.deepcopy(inp["context"]),
        catalog=eval_set.catalog,
        last_round=copy.deepcopy(inp.get("last_round")),
    )
    try:
        output = await subject.plan(planner_input)
    except Exception as exc:  # noqa: BLE001 — 被测对象的任何错误都记为这次决策失败，评测继续
        return _error(exc, split), None
    return score_planner(case, output, eval_set.catalog), output


async def _review(subject: Subject, cp: Checkpoint, split: str) -> tuple[DecisionScore, Any]:
    inp = ReviewerInput(
        task_state=copy.deepcopy(cp.task_state),
        stage=copy.deepcopy(cp.stage),
        results=copy.deepcopy(cp.results),
        history=copy.deepcopy(cp.history),
    )
    try:
        output = await subject.review(inp)
    except Exception as exc:  # noqa: BLE001
        return _error(exc, split), None
    return score_reviewer(cp, output), output


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_") or "subject"


def _report_name(eval_set: str, subject: str, split: str) -> str:
    return f"{_slug(eval_set)}--{_slug(subject)}--{split}--"


def latest_report(reports_dir: Path, *, eval_set: str, subject: str, split: str) -> Path | None:
    """reports_dir 里同一评测集、同一被测对象、同一划分的最近一份报告。"""
    if not reports_dir.is_dir():
        return None
    for path in sorted(reports_dir.glob(_report_name(eval_set, subject, split) + "*.json"), reverse=True):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("subject") == subject and data.get("split") == split and data.get("eval_set", {}).get("name") == eval_set:
            return path
    return None


def _flatten(metrics: dict[str, Any], prefix: str = "") -> dict[str, float]:
    flat: dict[str, float] = {}
    for key, value in metrics.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{name}."))
        elif isinstance(value, int | float) and not isinstance(value, bool):
            flat[name] = value
    return flat


def diff_reports(before: dict[str, Any], after: dict[str, Any], against: str | None = None) -> dict[str, Any]:
    """两份报告的差异：指标逐个比较；开发集还列出新失败与新通过的用例。"""
    old, new = _flatten(before.get("metrics", {})), _flatten(after.get("metrics", {}))
    metrics = {}
    for name in sorted(set(old) | set(new)):
        a, b = old.get(name), new.get(name)
        metrics[name] = {"before": a, "after": b, "delta": None if a is None or b is None else b - a}
    diff: dict[str, Any] = {
        "against": against,
        "comparable": before.get("eval_set", {}).get("version") == after.get("eval_set", {}).get("version"),
        "metrics": metrics,
    }
    if after.get("split") != "holdout" and "cases" in before and "cases" in after:
        was = {c["id"]: c["passed"] for c in before["cases"]}
        now = {c["id"]: c["passed"] for c in after["cases"]}
        diff["now_failing"] = sorted(i for i, ok in now.items() if not ok and was.get(i) is True)
        diff["now_passing"] = sorted(i for i, ok in now.items() if ok and was.get(i) is False)
    return diff


async def run_eval(
    eval_set: EvalSet,
    subject: Subject,
    *,
    split: str = "dev",
    judge: RubricJudge | None = None,
    judge_vendor: str | None = None,
    subject_vendor: str | None = None,
    reports_dir: Path | None = DEFAULT_REPORTS_DIR,
    compare: Path | str | None = LATEST,
    require_confirmed: bool = False,
) -> Report:
    """跑一遍 split 划分的用例并评分。

    judge_vendor 与 subject_vendor 是事先已知的供应商（例如命令行指定的模型），相同时直接拒绝；
    事后还会逐次核对 judge 实际用的供应商不在被测对象用过的供应商里。
    reports_dir 为 None 时不写文件；compare 为 None 时不比较。
    """
    if split not in SPLITS:
        raise EvalConfigError(f"划分只能是 {'、'.join(SPLITS)}，得到 {split!r}")
    if judge is not None and judge_vendor and subject_vendor and judge_vendor == subject_vendor:
        raise EvalConfigError(f"judge 与被测对象都是 {judge_vendor}：rubric 要用不同家的模型打分")
    es = eval_set.only(split)
    if require_confirmed and es.unconfirmed():
        raise UnconfirmedLabelsError(f"还有没经人确认的标注：{'、'.join(es.unconfirmed())}")
    declared = frozenset({subject_vendor}) if subject_vendor else frozenset()

    items: list[_ItemResult] = []
    plan_scores: dict[str, DecisionScore] = {}
    for case in es.task_cases:
        item = _ItemResult(id=case.id, kind="task", category=case.category)
        score, output = await _plan(subject, case, eval_set, split)
        plan_scores[case.id] = score
        item.decisions.append(_Decision("plan", score))
        for i, cp in enumerate(case.rounds, start=1):
            item.decisions.append(_Decision(f"round {i}", (await _review(subject, cp, split))[0]))
        if judge is not None and case.rubric and output is not None:
            material = {"message": case.planner_input["message"], "task_plan": output.task_plan, "stage": output.stage}
            rubric = await judge.score(criteria=case.rubric, material=material, subject_vendors=score.vendors | declared)
            item.rubric, item.rubric_cost = rubric.score, rubric.cost_usd
        items.append(item)
    for sample in es.reviewer_samples:
        item = _ItemResult(id=sample.id, kind="reviewer", category=sample.category)
        score, output = await _review(subject, sample.checkpoint, split)
        item.decisions.append(_Decision("review", score))
        if judge is not None and sample.rubric and output is not None:
            cp = sample.checkpoint
            material = {"stage": cp.stage, "results": cp.results, "task_state": cp.task_state, "verdict": output.verdict}
            rubric = await judge.score(criteria=sample.rubric, material=material, subject_vendors=score.vendors | declared)
            item.rubric, item.rubric_cost = rubric.score, rubric.cost_usd
        items.append(item)

    data = _build(eval_set, es, subject, split, items, plan_scores)
    previous: Path | None = None
    if compare == LATEST:
        if reports_dir is not None:
            previous = latest_report(reports_dir, eval_set=eval_set.name, subject=subject.name, split=split)
    elif compare is not None:
        previous = Path(compare)
    data["diff"] = diff_reports(json.loads(previous.read_text("utf-8")), data, str(previous)) if previous else None
    path = None
    if reports_dir is not None:
        reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = data["created_at"].replace("-", "").replace(":", "").replace("+0000", "Z")
        path = reports_dir / f"{_report_name(eval_set.name, subject.name, split)}{stamp}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    return Report(data=data, path=path)


def _build(eval_set: EvalSet, es: EvalSet, subject: Subject, split: str, items: list[_ItemResult], plan_scores: dict[str, DecisionScore]) -> dict[str, Any]:
    tasks = [i for i in items if i.kind == "task"]
    samples = [i for i in items if i.kind == "reviewer"]
    cases = {c.id: c for c in es.task_cases}
    plans = [plan_scores[t.id] for t in tasks]
    rounds = [d.score for t in tasks for d in t.decisions if d.label != "plan"]
    reviews = [d.score for s in samples for d in s.decisions]
    successes = sum(1 for t in tasks if t.passed)

    def triage_ok(case_id: str) -> bool:
        score = plan_scores[case_id]
        return not score.failed("triage") and not score.failed("error")

    def review_metrics(scores: list[DecisionScore]) -> dict[str, Any]:
        ok = [s for s in scores if not s.failed("verdict") and s.schema_ok]
        return {
            "decisions": len(scores),
            "passed": sum(1 for s in scores if s.passed),
            "verdict_accuracy": _rate(len(ok), len(scores)),
            "schema_pass_rate": _rate(sum(1 for s in scores if s.schema_ok), len(scores)),
        }

    task_cost = sum(t.cost for t in tasks)
    ratios = [
        plan_scores[t.id].cost_usd / cases[t.id].planner_expect["estimated_task_cost_usd"]
        for t in tasks
        if cases[t.id].planner_expect["triage"] == "full" and cases[t.id].planner_expect.get("estimated_task_cost_usd")
    ]
    rubric_scores = [i.rubric for i in items if i.rubric is not None]
    by_category: dict[str, dict[str, Any]] = {}
    for name in sorted({t.category or "" for t in tasks}):
        group = [t for t in tasks if t.category == name]
        won = sum(1 for t in group if t.passed)
        cost = sum(t.cost for t in group)
        by_category[name] = {"cases": len(group), "successes": won, "success_rate": _rate(won, len(group)), "cost_usd": cost, "per_success_usd": cost / won if won else None}
    by_triage: dict[str, dict[str, Any]] = {}
    for level in ("fast", "light", "full", "replan"):
        group = [t for t in tasks if cases[t.id].planner_expect["triage"] == level]
        if group:
            won = sum(1 for t in group if t.passed)
            by_triage[level] = {"cases": len(group), "triage_correct": sum(1 for t in group if triage_ok(t.id)), "successes": won, "success_rate": _rate(won, len(group))}
    reasons = Counter((f.dimension, f.code, f.message) for i in items for d in i.decisions for f in d.score.failures)
    labels = [*es.task_cases, *es.reviewer_samples]
    data: dict[str, Any] = {
        "eval_set": {"name": eval_set.name, "version": eval_set.version},
        "subject": subject.name,
        "split": split,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+0000"),
        "labels": {"confirmed": sum(1 for i in labels if i.confirmed), "draft": sum(1 for i in labels if not i.confirmed)},
        "metrics": {
            "success_rate": _rate(successes, len(tasks)),
            "planner": {
                "decisions": len(plans),
                "passed": sum(1 for s in plans if s.passed),
                "schema_pass_rate": _rate(sum(1 for s in plans if s.schema_ok), len(plans)),
                "triage_accuracy": _rate(sum(1 for t in tasks if triage_ok(t.id)), len(tasks)),
                "shape_match_rate": _rate(sum(1 for s in plans if s.schema_ok and not s.failed("shape")), len(plans)),
                "rule_violations": sum(1 for s in plans for f in s.failures if f.dimension == "rule"),
            },
            "rounds": review_metrics(rounds),
            "reviewer": review_metrics(reviews),
            "stuck_retry_errors": sum(1 for s in [*rounds, *reviews] for f in s.failures if f.dimension == "escalation"),
            "rubric": {"scored": len(rubric_scores), "mean": sum(rubric_scores) / len(rubric_scores) if rubric_scores else None},
            "cost": {
                "total_usd": sum(i.cost for i in items),
                "task_cases_usd": task_cost,
                "per_case_usd": task_cost / len(tasks) if tasks else None,
                "per_success_usd": task_cost / successes if successes else None,
                "judge_usd": sum(i.rubric_cost for i in items),
                "planner_cost_ratio": {"cases": len(ratios), "mean": sum(ratios) / len(ratios) if ratios else None, "max": max(ratios) if ratios else None},
            },
        },
        "breakdown": {"by_category": by_category, "by_triage": by_triage},
        "failure_reasons": [
            {"dimension": dim, "code": code, "message": message, "count": count}
            for (dim, code, message), count in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    }
    if split != "holdout":
        data["cases"] = [
            {
                "id": i.id,
                "kind": i.kind,
                "passed": i.passed,
                "failures": [{"decision": d.label, "dimension": f.dimension, "code": f.code, "message": f.message} for d in i.decisions for f in d.score.failures],
                "cost_usd": i.cost,
            }
            for i in sorted(items, key=lambda i: i.id)
        ]
    return data
