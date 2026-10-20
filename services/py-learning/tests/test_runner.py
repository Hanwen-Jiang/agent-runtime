"""S-08 运行器：一条命令跑完，输出成功率、成本，以及与上一版的差异；留出集只给汇总。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentruntime_learning.evals.__main__ import main
from agentruntime_learning.evals.cases import load_eval_set
from agentruntime_learning.evals.model import PlannerOutput, ReviewerInput, ReviewerOutput
from agentruntime_learning.evals.runner import EvalConfigError, diff_reports, run_eval

from support import ScriptedSubject, answer_stage, atomic, checkpoint, reviewer_sample, stage, task_case, usage, verdict

CONFIRMED = {"status": "confirmed", "confirmed_by": "reviewer", "confirmed_at": "2026-10-21"}


def full_plan(cost: float = 0.0, tier: str = "strong@medium") -> PlannerOutput:
    return PlannerOutput(triage_level="full", stage=answer_stage(), task_plan={"triage": {"level": "full"}, "horizon": "per_stage"}, usage=[usage(cost, tier=tier)] if cost else [])


def mixed_set(make_set) -> Path:
    """两条 general（full、fast）、一条 naming（full）、一条两轮的 research_report，外加两条 Reviewer 样本。"""
    two_rounds = [
        checkpoint("continue", stage_=stage(atomic("q", "sql_query@v1")), results={"q": {"role": "fetch", "status": "succeeded", "result": {"rows": 3}}}),
        checkpoint("done", stage_=answer_stage(seq=2)),
    ]
    return make_set(
        [
            task_case("g_full", estimated_cost=2.0),
            task_case("g_fast", triage="fast"),
            task_case("n_full", category="naming", estimated_cost=1.0),
            task_case("r_two_rounds", category="research_report", rounds=two_rounds),
            task_case("h_full", split="holdout", message="留出集里的一条消息，不该出现在报告里"),
        ],
        [reviewer_sample("s_done"), reviewer_sample("s_replan", name="replan", min_escalation="replan"), reviewer_sample("s_hold", split="holdout")],
    )


async def test_the_report_has_success_rate_cost_and_breakdowns(make_set) -> None:
    es = load_eval_set(mixed_set(make_set))
    subject = ScriptedSubject(plan_fn=lambda inp: full_plan(0.04), review_fn=lambda inp: ReviewerOutput(verdict=verdict("done"), usage=[usage(0.01)]))
    report = await run_eval(es, subject, reports_dir=None, compare=None)
    data = report.data
    assert data["split"] == "dev" and data["subject"] == "scripted"
    assert data["eval_set"] == {"name": es.name, "version": es.version}
    metrics = data["metrics"]
    # g_fast 的 Triage 错；r_two_rounds 第 1 轮该 continue 却判了 done。
    assert metrics["success_rate"] == pytest.approx(2 / 4)
    assert metrics["planner"]["decisions"] == 4 and metrics["planner"]["triage_accuracy"] == pytest.approx(3 / 4)
    assert metrics["planner"]["schema_pass_rate"] == 1.0
    assert metrics["rounds"]["decisions"] == 5 and metrics["rounds"]["verdict_accuracy"] == pytest.approx(4 / 5)
    assert metrics["reviewer"]["decisions"] == 2 and metrics["reviewer"]["verdict_accuracy"] == pytest.approx(1 / 2)
    cost = metrics["cost"]
    # 全部：4 次规划 + 5 个判定点 + 2 条样本；Task 用例的成本不含 Reviewer 样本。
    assert cost["total_usd"] == pytest.approx(4 * 0.04 + 7 * 0.01)
    assert cost["task_cases_usd"] == pytest.approx(4 * 0.04 + 5 * 0.01)
    assert cost["per_case_usd"] == pytest.approx(cost["task_cases_usd"] / 4)
    assert cost["per_success_usd"] == pytest.approx(cost["task_cases_usd"] / 2)
    by_category = data["breakdown"]["by_category"]
    assert by_category["general"]["cases"] == 2 and by_category["general"]["successes"] == 1
    assert by_category["research_report"]["successes"] == 0
    assert by_category["naming"]["per_success_usd"] == pytest.approx(by_category["naming"]["cost_usd"])
    assert data["breakdown"]["by_triage"]["fast"] == {"cases": 1, "triage_correct": 0, "successes": 0, "success_rate": 0.0}
    assert [c["id"] for c in data["cases"]] == ["g_fast", "g_full", "n_full", "r_two_rounds", "s_done", "s_replan"]


async def test_planning_cost_is_reported_as_a_share_of_the_estimated_task_cost(make_set) -> None:
    es = load_eval_set(mixed_set(make_set))
    report = await run_eval(es, ScriptedSubject(plan_fn=lambda inp: full_plan(0.05)), reports_dir=None, compare=None)
    ratio = report.data["metrics"]["cost"]["planner_cost_ratio"]
    # 只有标了预计成本的 full 用例：g_full 0.05/2.0，n_full 0.05/1.0。
    assert ratio == {"cases": 2, "mean": pytest.approx((0.025 + 0.05) / 2), "max": pytest.approx(0.05)}


async def test_failure_reasons_explain_the_score(make_set) -> None:
    es = load_eval_set(mixed_set(make_set))
    report = await run_eval(es, ScriptedSubject(), reports_dir=None, compare=None)
    reasons = report.data["failure_reasons"]
    assert reasons == sorted(reasons, key=lambda r: -r["count"])
    assert {"triage", "verdict"} <= {r["dimension"] for r in reasons}
    summary = report.summary()
    for text in ("成功率", "成本", "失败原因", "general", "research_report"):
        assert text in summary
    assert any(r["message"] in summary for r in reasons)


async def test_rounds_are_judged_on_the_labelled_state_not_on_the_subjects_plan(make_set) -> None:
    """逐个判定点评测：Reviewer 拿到的是标注里的 Stage 与结果，而且只有 Stage、结果、Task State 与之前的摘要。"""
    es = load_eval_set(mixed_set(make_set))
    odd_stage = stage(atomic("x", "sql_query@v1"))
    subject = ScriptedSubject(plan_fn=lambda inp: PlannerOutput(triage_level="full", stage=odd_stage, task_plan={"triage": {"level": "full"}, "horizon": "per_stage"}))
    await run_eval(es, subject, reports_dir=None, compare=None)
    assert all(inp.stage != odd_stage for inp in subject.review_inputs)
    assert {tuple(n["id"] for n in inp.stage["nodes"]) for inp in subject.review_inputs} == {("answer",), ("q",)}
    assert set(ReviewerInput.__dataclass_fields__) == {"task_state", "stage", "results", "history"}
    for inp in [*subject.plan_inputs, *subject.review_inputs]:
        assert not any(case_id in repr(inp) for case_id in ("g_full", "g_fast", "n_full", "r_two_rounds", "s_done")), "被测对象拿不到用例 id"


async def test_a_task_case_succeeds_only_if_the_plan_and_every_round_pass(make_set) -> None:
    es = load_eval_set(mixed_set(make_set)).only("dev")
    report = await run_eval(es, ScriptedSubject(), reports_dir=None, compare=None)
    [two_rounds] = [c for c in report.data["cases"] if c["id"] == "r_two_rounds"]
    assert not two_rounds["passed"]
    assert any(f["dimension"] == "verdict" for f in two_rounds["failures"])


async def test_reports_are_written_and_compared_with_the_previous_run(make_set, tmp_path: Path) -> None:
    es = load_eval_set(mixed_set(make_set))
    reports = tmp_path / "reports"
    first = await run_eval(es, ScriptedSubject(), reports_dir=reports)
    assert first.path is not None and first.path.exists() and first.data["diff"] is None
    assert json.loads(first.path.read_text("utf-8"))["metrics"] == first.data["metrics"]
    second =await run_eval(es, ScriptedSubject(review_fn=lambda inp: ReviewerOutput(verdict=verdict("continue"))), reports_dir=reports)
    diff = second.data["diff"]
    assert diff["against"] == str(first.path) and diff["comparable"] is True
    assert diff["metrics"]["rounds.verdict_accuracy"]["delta"] == pytest.approx(
        second.data["metrics"]["rounds"]["verdict_accuracy"] - first.data["metrics"]["rounds"]["verdict_accuracy"]
    )
    assert "g_full" in diff["now_failing"] and "r_two_rounds" not in diff["now_passing"]
    assert "与上一版" in second.summary()


async def test_compare_can_be_disabled_or_pointed_at_a_report(make_set, tmp_path: Path) -> None:
    es = load_eval_set(mixed_set(make_set))
    reports = tmp_path / "reports"
    first = await run_eval(es, ScriptedSubject(), reports_dir=reports)
    assert (await run_eval(es, ScriptedSubject(), reports_dir=reports, compare=None)).data["diff"] is None
    explicit = await run_eval(es, ScriptedSubject(), reports_dir=None, compare=first.path)
    assert explicit.data["diff"]["against"] == str(first.path)
    other = await run_eval(es, ScriptedSubject(name="other"), reports_dir=reports)
    assert other.data["diff"] is None, "latest 只找同一被测对象的报告"


def test_reports_of_another_eval_set_version_are_not_comparable() -> None:
    base = {"eval_set": {"name": "task-loop", "version": "a" * 64}, "split": "dev", "metrics": {"success_rate": 0.5}, "cases": []}
    changed = {**base, "eval_set": {"name": "task-loop", "version": "b" * 64}, "metrics": {"success_rate": 0.6}}
    diff = diff_reports(base, changed)
    assert diff["comparable"] is False
    assert diff["metrics"]["success_rate"]["delta"] == pytest.approx(0.1)


async def test_the_holdout_report_only_has_aggregates(make_set, tmp_path: Path) -> None:
    es = load_eval_set(mixed_set(make_set))
    reports = tmp_path / "reports"
    await run_eval(es, ScriptedSubject(), split="holdout", reports_dir=reports)
    report = await run_eval(es, ScriptedSubject(review_fn=lambda inp: ReviewerOutput(verdict=verdict("continue"))), split="holdout", reports_dir=reports)
    assert report.data["split"] == "holdout"
    assert "cases" not in report.data
    assert "now_failing" not in report.data["diff"] and "now_passing" not in report.data["diff"]
    text = json.dumps(report.data, ensure_ascii=False) + report.summary()
    for leaked in ("h_full", "s_hold", "留出集里的一条消息"):
        assert leaked not in text


async def test_the_default_split_is_dev(make_set) -> None:
    es = load_eval_set(mixed_set(make_set))
    subject = ScriptedSubject()
    report = await run_eval(es, subject, reports_dir=None, compare=None)
    assert report.data["split"] == "dev"
    assert all("留出集" not in inp.message for inp in subject.plan_inputs)


async def test_labels_must_be_confirmed_when_required(make_set) -> None:
    es = load_eval_set(make_set([task_case("t_draft"), task_case("t_ok", review=CONFIRMED)]))
    report = await run_eval(es, ScriptedSubject(), reports_dir=None, compare=None)
    assert report.data["labels"] == {"confirmed": 1, "draft": 1}
    assert "待确认" in report.summary()
    with pytest.raises(EvalConfigError, match="t_draft"):
        await run_eval(es, ScriptedSubject(), reports_dir=None, compare=None, require_confirmed=True)


async def test_a_subject_error_fails_that_decision_and_the_run_goes_on(make_set) -> None:
    def flaky(inp):
        if "第二" in inp.message:
            raise RuntimeError("模型超时")
        return full_plan()

    cases = [task_case("t_one"), task_case("t_two", message="第二条消息")]
    es = load_eval_set(make_set(cases))
    report = await run_eval(es, ScriptedSubject(plan_fn=flaky), reports_dir=None, compare=None)
    [failed] = [c for c in report.data["cases"] if c["id"] == "t_two"]
    assert not failed["passed"] and failed["failures"][0]["code"] == "subject_error"
    assert report.data["metrics"]["success_rate"] == pytest.approx(0.5)


# --- 命令行 ---


def test_one_command_runs_the_eval(make_set, tmp_path: Path, capsys) -> None:
    root = mixed_set(make_set)
    reports = tmp_path / "reports"
    code = main(["run", "--eval-set", str(root), "--subject", "baseline", "--reports-dir", str(reports)])
    out = capsys.readouterr().out
    assert code == 0
    assert "成功率" in out and "成本" in out
    [written] = list(reports.glob("*.json"))
    assert json.loads(written.read_text("utf-8"))["subject"] == "baseline"


def test_the_cli_accepts_a_subject_factory(make_set, tmp_path: Path) -> None:
    root = mixed_set(make_set)
    code = main(["run", "--eval-set", str(root), "--subject", "agentruntime_learning.evals.subjects:BaselineSubject", "--reports-dir", str(tmp_path), "--compare", "none"])
    assert code == 0


def test_the_cli_reports_bad_arguments(make_set, tmp_path: Path, capsys) -> None:
    root = mixed_set(make_set)
    assert main(["run", "--eval-set", str(root), "--subject", "no_such_subject", "--reports-dir", str(tmp_path)]) == 2
    assert main(["run", "--eval-set", str(tmp_path / "missing"), "--subject", "baseline", "--reports-dir", str(tmp_path)]) == 2
    assert main(["run", "--eval-set", str(root), "--subject", "baseline", "--split", "test", "--reports-dir", str(tmp_path)]) == 2


def test_the_cli_refuses_draft_labels_when_asked(make_set, tmp_path: Path, capsys) -> None:
    root = make_set([task_case("t_draft")])
    code = main(["run", "--eval-set", str(root), "--subject", "baseline", "--reports-dir", str(tmp_path), "--require-confirmed"])
    assert code == 3
    assert "t_draft" in capsys.readouterr().err
