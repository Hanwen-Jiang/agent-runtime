"""T-02、T-03：同一被测对象重复跑评测，指标给中位数与范围，S-09、S-10 的门槛按中位数判。

真模型有随机性、样本又少，单次差一两题不下结论（TMP 计划第 4 节风险 1）：
- 每次的报告照常落盘（保留单次结果），另写一份汇总；
- 汇总里每个数值指标给 values、median、min、max；
- 门槛（S-09：Triage 准确率 ≥ 90%、Stage schema 通过率 100%、规则违例 0、replan 不重做已验证的 Node、
  规划成本不超过 Task 预计成本的 5%；S-10：Reviewer 样本 Verdict 准确率 ≥ 85%、“该升级却原地重试”为 0、
  Reviewer 与判定点的 schema 通过率 100%）按中位数判，未达标的写出差距；没有数据的指标算未达标。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentruntime_learning.evals.__main__ import main
from agentruntime_learning.evals.cases import load_eval_set
from agentruntime_learning.evals.gates import GATES, evaluate_gate
from agentruntime_learning.evals.model import PlannerOutput, ReviewerOutput
from agentruntime_learning.evals.repeat import run_repeated

from support import ScriptedSubject, answer_stage, reviewer_sample, task_case, usage, verdict


def plan(level: str, cost: float = 0.04) -> PlannerOutput:
    return PlannerOutput(triage_level=level, stage=answer_stage(), task_plan={"triage": {"level": level}, "horizon": "per_stage"}, usage=[usage(cost, tier="strong@medium")])


def triage_by_run(right: list[bool], cases_per_run: int) -> ScriptedSubject:
    """第 i 次跑（从 0 数）的 Triage 对不对由 right[i] 决定：对就答 full，错就答 light。"""
    calls = {"n": 0}

    def plan_fn(_inp):  # type: ignore[no-untyped-def]
        run = calls["n"] // cases_per_run
        calls["n"] += 1
        return plan("full" if right[run] else "light")

    return ScriptedSubject(plan_fn=plan_fn)


def two_full_cases(make_set) -> Path:
    return make_set([task_case("t1", estimated_cost=2.0), task_case("t2", estimated_cost=4.0), task_case("h1", split="holdout", message="留出集里的消息", estimated_cost=1.0)])


async def test_every_run_is_kept_and_the_aggregate_gives_median_and_range(make_set, tmp_path: Path) -> None:
    es = load_eval_set(two_full_cases(make_set))
    reports = tmp_path / "reports"
    result = await run_repeated(es, triage_by_run([False, True, True], 2), split="dev", repeat=3, reports_dir=reports, compare=None)
    assert len(result.runs) == 3
    assert all(run.path is not None and run.path.is_file() for run in result.runs), "单次报告照常落盘"
    assert result.path is not None and result.path.is_file() and result.path not in {r.path for r in result.runs}
    data = json.loads(result.path.read_text("utf-8"))
    assert data == result.data
    assert data["repeat"] == 3 and data["runs"] == [str(r.path) for r in result.runs]
    assert data["metrics"]["planner.triage_accuracy"] == {"values": [0.0, 1.0, 1.0], "median": 1.0, "min": 0.0, "max": 1.0}
    assert data["metrics"]["planner.decisions"]["median"] == 2


@pytest.mark.parametrize(("right", "passed"), [([False, True, True], True), ([True, False, False], False)])
async def test_the_s09_gate_is_judged_on_the_median(make_set, right: list[bool], passed: bool) -> None:
    es = load_eval_set(two_full_cases(make_set))
    result = await run_repeated(es, triage_by_run(right, 2), split="dev", repeat=3, reports_dir=None, compare=None, gate="s09")
    gate = result.data["gate"]
    assert gate["name"] == "s09"
    triage = next(c for c in gate["checks"] if c["metric"] == "planner.triage_accuracy")
    assert triage["passed"] is passed and gate["passed"] is passed
    if not passed:
        assert triage["gap"], "未达标写出差距"
        assert "未达标" in result.summary() and "Triage" in result.summary()


async def test_the_s09_gate_covers_every_planner_threshold(make_set) -> None:
    es = load_eval_set(two_full_cases(make_set))
    result = await run_repeated(es, triage_by_run([True], 2), split="dev", repeat=1, reports_dir=None, compare=None, gate="s09")
    checks = {c["metric"]: c for c in result.data["gate"]["checks"]}
    assert set(checks) == {
        "planner.triage_accuracy",
        "planner.schema_pass_rate",
        "planner.rule_violations",
        "failures.keep_nodes",
        "cost.planner_cost_ratio.max",
    }
    # 规划 0.04 美元，预计 2.0 与 4.0 美元：比例最大 2%，不超过 5%。
    assert checks["cost.planner_cost_ratio.max"]["median"] == pytest.approx(0.02) and checks["cost.planner_cost_ratio.max"]["passed"]
    assert checks["failures.keep_nodes"]["median"] == 0 and checks["failures.keep_nodes"]["passed"], "没有这类失败按 0 算"
    assert result.data["gate"]["passed"]


async def test_planning_that_costs_too_much_fails_the_s09_gate(make_set) -> None:
    es = load_eval_set(two_full_cases(make_set))
    subject = ScriptedSubject(plan_fn=lambda _inp: plan("full", cost=0.5))
    result = await run_repeated(es, subject, split="dev", repeat=1, reports_dir=None, compare=None, gate="s09")
    cost = next(c for c in result.data["gate"]["checks"] if c["metric"] == "cost.planner_cost_ratio.max")
    assert not cost["passed"] and cost["median"] == pytest.approx(0.25)


async def test_a_metric_without_data_fails_its_check(make_set) -> None:
    """没有带预计成本的 full 用例，规划成本占比算不出来：不当作达标。"""
    es = load_eval_set(make_set([task_case("t1")]))
    result = await run_repeated(es, ScriptedSubject(), split="dev", repeat=1, reports_dir=None, compare=None, gate="s09")
    cost = next(c for c in result.data["gate"]["checks"] if c["metric"] == "cost.planner_cost_ratio.max")
    assert cost["median"] is None and not cost["passed"] and "无数据" in cost["gap"]
    assert not result.data["gate"]["passed"]


async def test_the_s10_gate_catches_retrying_in_place(make_set) -> None:
    samples = [reviewer_sample("s_replan", name="replan", min_escalation="replan"), reviewer_sample("s_done")]
    es = load_eval_set(make_set([task_case("t1", estimated_cost=1.0)], samples))
    stuck = ScriptedSubject(review_fn=lambda _inp: ReviewerOutput(verdict=verdict("repair")))
    result = await run_repeated(es, stuck, split="dev", repeat=2, reports_dir=None, compare=None, gate="s10")
    checks = {c["metric"]: c for c in result.data["gate"]["checks"]}
    assert set(checks) == {"reviewer.verdict_accuracy", "stuck_retry_errors", "reviewer.schema_pass_rate", "rounds.schema_pass_rate"}
    assert not checks["stuck_retry_errors"]["passed"] and checks["stuck_retry_errors"]["median"] >= 1
    assert not checks["reviewer.verdict_accuracy"]["passed"]
    assert not result.data["gate"]["passed"]


def test_gate_thresholds_match_the_plan() -> None:
    thresholds = {name: {c.metric: (c.op, c.threshold) for c in checks} for name, checks in GATES.items()}
    assert thresholds["s09"]["planner.triage_accuracy"] == (">=", 0.9)
    assert thresholds["s09"]["planner.schema_pass_rate"] == (">=", 1.0)
    assert thresholds["s09"]["planner.rule_violations"] == ("<=", 0)
    assert thresholds["s09"]["cost.planner_cost_ratio.max"] == ("<=", 0.05)
    assert thresholds["s10"]["reviewer.verdict_accuracy"] == (">=", 0.85)
    assert thresholds["s10"]["stuck_retry_errors"] == ("<=", 0)


def test_evaluate_gate_reads_medians_from_an_aggregate() -> None:
    metrics = {"reviewer.verdict_accuracy": {"values": [0.8, 0.9, 0.9], "median": 0.9, "min": 0.8, "max": 0.9}, "stuck_retry_errors": {"values": [0, 1, 0], "median": 0, "min": 0, "max": 1},
               "reviewer.schema_pass_rate": {"values": [1.0], "median": 1.0, "min": 1.0, "max": 1.0}, "rounds.schema_pass_rate": {"values": [1.0], "median": 1.0, "min": 1.0, "max": 1.0}}
    gate = evaluate_gate("s10", metrics)
    assert gate["passed"]
    stuck = next(c for c in gate["checks"] if c["metric"] == "stuck_retry_errors")
    assert stuck["max"] == 1, "范围照样给出，单次出过的违例看得见"


async def test_holdout_aggregates_carry_no_case_level_detail(make_set, tmp_path: Path) -> None:
    es = load_eval_set(two_full_cases(make_set))
    result = await run_repeated(es, ScriptedSubject(), split="holdout", repeat=2, reports_dir=tmp_path, compare=None, gate="s09")
    text = json.dumps(result.data, ensure_ascii=False) + result.summary()
    assert "h1" not in text and "留出集里的消息" not in text


def test_the_cli_repeats_runs_and_prints_the_gate(make_set, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = two_full_cases(make_set)
    reports = tmp_path / "reports"
    code = main(["run", "--eval-set", str(root), "--subject", "baseline", "--repeat", "2", "--gate", "s09", "--reports-dir", str(reports), "--compare", "none"])
    out = capsys.readouterr().out
    assert code == 0
    assert len(list(reports.glob("*.json"))) == 3, "两份单次报告加一份汇总"
    assert "中位数" in out and "s09" in out


@pytest.mark.parametrize("args", [["--repeat", "0"], ["--repeat", "x"], ["--gate", "s99"]])
def test_the_cli_rejects_bad_repeat_or_gate(make_set, tmp_path: Path, args: list[str]) -> None:
    root = two_full_cases(make_set)
    reports = tmp_path / "reports"
    assert main(["run", "--eval-set", str(root), "--subject", "baseline", "--compare", "none", "--reports-dir", str(reports), *args]) == 2
    assert not reports.exists(), "参数不对时一次也不跑"
