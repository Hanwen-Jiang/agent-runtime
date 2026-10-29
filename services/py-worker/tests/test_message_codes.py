"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 5 节验收：代码生成的说明都带代码与参数，客户端按代码翻译。

细化见该计划第 11.5 节的代码表与第 11.8 节：
- 规则给出或改写的 Verdict 带 reason_code 与 reason_params；取决定最终去向的那一条规则的代码。模型自己的理由、规则没改的不带。
- 问人的问题由代码生成时，human_input_required 的 question_code 与 Verdict 的 reason_code 相同；模型写的问题不带。
- 问人后停止或没回复，工作流推一个 abort 的 verdict 事件，reason_code 是 human_stopped、human_no_reply；before_finish
  驳回用满上限后的问人是 finish_rejected。
- Node 失败的说明写成“代码：说明”的，node_finished 带 error_code（budget_check 记为 budget_exhausted、budget 为 usd），
  上游没成功而跳过的是 upstream_failed；tool_loop 里工具的错误写成“代码: 说明”的，tool_status 带 error_code。
- fast 规划的固定 goal 带 goal_code reply_to_message（PlanRoundOutput.goal_code，stage_started 带出）。
"""

from __future__ import annotations

from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from agentruntime.common.v1 import enums_pb2

from agentruntime_worker.activities.types import StagePolicy, ToolLoopStep
from agentruntime_worker.planner.activities import PlannerActivities
from agentruntime_worker.planner.context import StaticContextSource
from agentruntime_worker.planner.planner import Planner
from agentruntime_worker.reviewer.rules import Caps
from agentruntime_worker.workflows.task import _finish_escalation

from .planner_support import FakeGateway, context, signals
from .reviewer_support import HUMAN_REPLIES, answer, atomic, done, failed, goal, ok, past, retry, rounds, stage, task_state
from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .support import assert_valid
from .test_planner_activity import plan_input
from .test_reviewer_rules import LINT_FAIL, LINT_STAGE, decide, lint_history
from .test_stage_runner import deliver, one_round, stage as run_stage
from .test_stage_runner import goal as run_goal
from .test_task_verdicts import out, reply_with, scripted

temporal = (pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session"))


def marked(fn: Any) -> Any:
    for mark in temporal:
        fn = mark(fn)
    return fn


# --- Reviewer 的规则 ---


def test_a_proposal_the_rules_keep_has_no_code() -> None:
    d, _, _ = decide(LINT_STAGE, LINT_FAIL, retry("fix_lint"))
    assert "reason_code" not in d.verdict and "reason_params" not in d.verdict


def test_the_second_failure_of_a_node_is_coded_with_the_node() -> None:
    d, _, _ = decide(LINT_STAGE, LINT_FAIL, retry("fix_lint"), history=lint_history(1))
    assert d.verdict["reason_code"] == "rule_failed_twice"
    assert d.verdict["reason_params"] == {"node": "lint", "times": 2}


def test_the_third_failure_of_a_node_is_coded_with_the_node() -> None:
    d, _, _ = decide(LINT_STAGE, LINT_FAIL, retry("fix_lint"), history=lint_history(2))
    assert d.verdict["verdict"] == "replan"
    assert d.verdict["reason_code"] == "rule_failed_thrice" and d.verdict["reason_params"] == {"node": "lint", "times": 3}


def test_a_used_up_step_is_coded_with_the_cap() -> None:
    d, _, _ = decide(LINT_STAGE, LINT_FAIL, retry("fix_lint"), caps=Caps(repair_retry=0))
    assert d.verdict["repair"]["strategy"] == "change_method"
    assert d.verdict["reason_code"] == "rule_cap_reached"
    assert d.verdict["reason_params"] == {"step": "repair_retry", "used": 0, "cap": 0, "next": "repair_change_method"}


def test_a_denied_node_is_coded() -> None:
    s = stage(atomic("pay", role="act", effect="write_reversible"))
    d, _, _ = decide(s, {"pay": failed("超过 effect 上限", role="act", failure_class="denied")}, retry("pay"))
    assert d.verdict["reason_code"] == "rule_denied" and d.verdict["reason_params"] == {"nodes": ["pay"]}


def test_done_against_the_own_progress_check_is_coded() -> None:
    s = stage(atomic("answer", role="deliver"))
    proposal = answer({"verdict": "done", "done": {"deliver_node": "answer"}, "progress_check": {
        "criteria_met": False, "progress": True, "looping": False, "assumptions_hold": True, "next_step": "补数据"}})
    d, _, _ = decide(s, {"answer": ok("deliver")}, proposal)
    assert d.verdict["verdict"] == "continue" and d.verdict["reason_code"] == "rule_criteria_not_met"


def test_done_without_a_delivered_answer_is_coded() -> None:
    s = stage(goal("explore"))
    d, _, _ = decide(s, {"explore": ok()}, done("explore"))
    assert d.verdict["verdict"] == "continue" and d.verdict["reason_code"] == "rule_no_deliver"


def test_rollback_to_an_unknown_checkpoint_is_coded() -> None:
    history = [past(1, "continue")]
    proposal = {"verdict": {"verdict": "rollback", "reason": "账户被清空", "rollback": {"checkpoint": "round_9"}}}
    d, _, _ = decide(stage(atomic("e2e", role="judge")), {"e2e": failed("accounts.db 记录数为 0", role="judge")}, proposal, history=history)
    assert d.verdict["reason_code"] == "rule_no_checkpoint" and d.verdict["reason_params"] == {"checkpoint": "round_9"}


def test_reroutes_the_rules_refuse_are_coded() -> None:
    s = stage(goal("explore"))
    results = {"explore": ok(result={"finding": "适合别的流程"})}
    unknown, _, _ = decide(s, results, {"verdict": {"verdict": "reroute", "reason": "换流程", "reroute": {"recipe": "made_up@v1"}}})
    assert unknown.verdict["reason_code"] == "rule_unknown_recipe" and unknown.verdict["reason_params"] == {"recipe": "made_up@v1"}
    risky, _, _ = decide(s, results, {"verdict": {"verdict": "reroute", "reason": "换流程", "reroute": {"recipe": "flight_booking@v1"}}})
    assert risky.verdict["reason_code"] == "rule_recipe_over_ceiling"
    assert risky.verdict["reason_params"]["recipe"] == "flight_booking@v1" and set(risky.verdict["reason_params"]) == {"recipe", "risk", "ceiling"}


def test_a_spent_budget_is_coded() -> None:
    state = task_state(rounds=rounds("continue", "continue", costs=[0.9, 0.8], current_cost=0.35), budget=2.0)
    s = stage(goal("fix_lint"))
    d, _, _ = decide(s, {"fix_lint": ok()}, {"verdict": {"verdict": "continue", "reason": "继续"}}, history=[past(1, "continue"), past(2, "continue")], state=state)
    assert d.verdict["verdict"] == "escalate" and d.verdict["reason_code"] == "rule_budget_spent"
    assert "spent_ratio" in d.verdict["reason_params"]


@pytest.mark.parametrize("history", [[], [past(1, "continue")]])
def test_every_code_is_snake_case_and_valid(history: list[dict[str, Any]]) -> None:
    for proposal in (None, retry("fix_lint"), done("fix_lint")):
        d, _, _ = decide(LINT_STAGE, LINT_FAIL, proposal, history=history)
        assert_valid("verdict", d.verdict)


def test_the_finish_escalation_is_coded() -> None:
    verdict = _finish_escalation("缺少引用", 3)
    assert verdict["reason_code"] == "finish_rejected" and verdict["reason_params"] == {"count": 3}
    assert_valid("verdict", verdict)


# --- 工作流：问题、停止与 Node 的失败 ---


def events(h: StageHarness, task_id: str, kind: str) -> list[dict[str, Any]]:
    return [e for e in h.script.progress(task_id) if e["event"] == kind]


ESCALATE = {"target": "human", "question": "按规则改为问人：……继续吗？", "options": list(HUMAN_REPLIES), "state_summary": "x"}


@marked
async def test_a_question_written_by_the_rules_carries_their_code(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [run_stage(1, deliver("answer")), run_stage(2, deliver("answer2"))]
    s.reviewer = scripted(out("escalate", "风险超出上限", escalate=ESCALATE, reason_code="rule_recipe_over_ceiling",
                              reason_params={"recipe": "flight_booking@v1", "risk": "write_irreversible", "ceiling": "read"}))
    s.on_question = reply_with(harness, "stop_and_wrap_up")
    inp, result = await harness.run()
    asked = events(harness, inp.task_id, "human_input_required")
    assert asked[0]["question_code"] == "rule_recipe_over_ceiling" and asked[0]["params"]["recipe"] == "flight_booking@v1"
    assert result.status == enums_pb2.TASK_STATUS_ABORTED
    last = events(harness, inp.task_id, "verdict")[-1]
    assert last["verdict"] == "abort" and last["reason_code"] == "human_stopped"
    for e in harness.script.progress(inp.task_id):
        assert_valid("task_event_payload", e)


@marked
async def test_a_question_the_model_wrote_has_no_code_and_no_reply_is_coded(harness: StageHarness) -> None:
    s = harness.script
    s.policy = StagePolicy(human_timeout_s=1)
    s.plans["main"] = [run_stage(1, deliver("answer"))]
    s.reviewer = scripted(out("escalate", "要问人", escalate={**ESCALATE, "question": "是否按原方案出票？"}))
    inp, result = await harness.run()
    asked = events(harness, inp.task_id, "human_input_required")
    assert "question_code" not in asked[0] and "params" not in asked[0]
    last = events(harness, inp.task_id, "verdict")[-1]
    assert result.status == enums_pb2.TASK_STATUS_ABORTED and last["reason_code"] == "human_no_reply"


def use_repo_read(h: StageHarness, produce: Any) -> None:
    h.script.tools["repo_read"] = "read"
    h.script.tool_results["repo_read"] = produce


def finished(h: StageHarness, task_id: str, node_id: str) -> dict[str, Any]:
    return next(e for e in events(h, task_id, "node_finished") if e["node_id"] == node_id)


@marked
async def test_node_failures_written_as_code_and_text_carry_the_code(harness: StageHarness) -> None:
    s = harness.script
    use_repo_read(harness, lambda i: {"content": "x"})
    s.tool_steps["explore"] = lambda inp: ToolLoopStep(action="tool", tool="repo_read", args={"path": f"f{inp.iteration}.py"})
    one_round(harness, run_goal("explore", "tool_loop", tools=["repo_read"], max_iterations=2), deliver(after=["explore"]))
    inp, _ = await harness.run()
    explore = finished(harness, inp.task_id, "explore")
    assert explore["error_code"] == "max_iterations"
    skipped = finished(harness, inp.task_id, "answer")
    assert skipped["status"] == "skipped" and skipped["error_code"] == "upstream_failed" and skipped["params"] == {"node": "explore"}


@marked
async def test_a_repeated_tool_error_is_coded_and_the_tool_status_carries_the_tools_code(harness: StageHarness) -> None:
    s = harness.script
    use_repo_read(harness, lambda i: FileNotFoundError("file_not_found: 工作区里没有文件 README.md"))
    s.tool_steps["explore"] = lambda inp: ToolLoopStep(action="tool", tool="repo_read", args={"path": "README.md"})
    one_round(harness, run_goal("explore", "tool_loop", tools=["repo_read"], max_iterations=10), deliver(after=["explore"]))
    inp, _ = await harness.run()
    failed_calls = [e for e in events(harness, inp.task_id, "tool_status") if e["state"] == "failed"]
    assert failed_calls and all(e["error_code"] == "file_not_found" for e in failed_calls)
    assert finished(harness, inp.task_id, "explore")["error_code"] == "repeated_error"


@marked
async def test_the_dollar_budget_stop_is_coded_like_the_other_budgets(harness: StageHarness) -> None:
    s = harness.script
    s.plan_cost = 3.0
    use_repo_read(harness, lambda i: {"content": "x"})
    s.tool_steps["explore"] = lambda inp: ToolLoopStep(action="finish", result={})
    one_round(harness, run_goal("explore", "tool_loop", tools=["repo_read"], max_iterations=3), deliver(after=["explore"]))
    inp, _ = await harness.run()
    explore = finished(harness, inp.task_id, "explore")
    assert explore["error_code"] == "budget_exhausted"
    assert explore["params"]["budget"] == "usd" and explore["params"]["limit"] == 2.0 and explore["params"]["used"] >= 3.0


@marked
async def test_the_fast_goal_is_coded_in_stage_started(harness: StageHarness) -> None:
    s = harness.script
    s.goal_code = "reply_to_message"
    one_round(harness, deliver("answer"))
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    started = events(harness, inp.task_id, "stage_started")[0]
    assert started["goal_code"] == "reply_to_message"


# --- Planner：fast 的 goal ---


async def test_a_fast_plan_returns_the_goal_code() -> None:
    gateway = FakeGateway(triage=signals("follow_up", family=None, effect="none"))
    out_ = await ActivityEnvironment().run(PlannerActivities(Planner(gateway), StaticContextSource(context())).plan_round, plan_input("再短一点"))
    assert out_.triage_level == "fast" and out_.goal_code == "reply_to_message"


async def test_a_planned_goal_has_no_goal_code() -> None:
    from .planner_support import plan_answer

    gateway = FakeGateway(triage=signals(confidence=0.6), plan=[plan_answer()])
    out_ = await ActivityEnvironment().run(PlannerActivities(Planner(gateway), StaticContextSource(context())).plan_round, plan_input())
    assert out_.triage_level == "full" and out_.goal_code == ""
