"""S-08 验收：用“永远返回 full + 单个 Node”的基线去跑，运行器能给出可解释的低分；参照输出得满分（标注自洽）。"""

from __future__ import annotations

import pytest

from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET, load_eval_set
from agentruntime_learning.evals.runner import run_eval
from agentruntime_learning.evals.subjects import BaselineSubject, OracleSubject, load_subject


@pytest.mark.parametrize("split", ["dev", "holdout"])
async def test_the_reference_outputs_score_full_marks(split: str) -> None:
    es = load_eval_set(DEFAULT_EVAL_SET)
    report = await run_eval(es, OracleSubject(es), split=split, reports_dir=None, compare=None)
    metrics = report.data["metrics"]
    assert metrics["success_rate"] == 1.0, report.summary()
    assert metrics["planner"]["schema_pass_rate"] == 1.0 and metrics["planner"]["rule_violations"] == 0
    assert metrics["rounds"]["verdict_accuracy"] == 1.0 and metrics["reviewer"]["verdict_accuracy"] == 1.0
    assert metrics["stuck_retry_errors"] == 0


async def test_the_baseline_gets_an_explainable_low_score() -> None:
    es = load_eval_set(DEFAULT_EVAL_SET)
    report = await run_eval(es, BaselineSubject(), reports_dir=None, compare=None)
    data, metrics = report.data, report.data["metrics"]
    dev_cases = [c for c in es.task_cases if c.split == "dev"]
    full_share = sum(1 for c in dev_cases if c.planner_expect["triage"] == "full") / len(dev_cases)

    # 低分：
    assert metrics["success_rate"] < 0.3, report.summary()
    assert metrics["planner"]["triage_accuracy"] == pytest.approx(full_share), "基线永远 full，只在标为 full 的用例上对"
    assert metrics["stuck_retry_errors"] > 0, "基线的 Reviewer 有失败就原地重试"
    # 可解释：输出本身结构合格，分低在内容；失败原因、分级明细说得清低在哪里。
    assert metrics["planner"]["schema_pass_rate"] == 1.0
    dimensions = {r["dimension"] for r in data["failure_reasons"]}
    assert {"triage", "shape", "verdict", "escalation"} <= dimensions
    by_triage = data["breakdown"]["by_triage"]
    assert all(by_triage[level]["triage_correct"] == 0 for level in ("fast", "light", "replan"))
    summary = report.summary()
    assert "失败原因" in summary and "fast" in summary
    assert data["failure_reasons"][0]["message"] in summary


def test_the_baseline_is_available_by_name() -> None:
    es = load_eval_set(DEFAULT_EVAL_SET)
    assert load_subject("baseline", es).name == "baseline"
    assert load_subject("oracle", es).name == "oracle"
