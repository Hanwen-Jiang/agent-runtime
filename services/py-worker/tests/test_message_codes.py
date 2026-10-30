"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 5 节验收：代码生成的说明都带代码与参数，客户端按代码翻译。

细化见该计划第 11.5 节的代码表与第 11.8 节：
- 规则给出或改写的 Verdict 带 reason_code 与 reason_params；取决定最终去向的那一条规则的代码。模型自己的理由、规则没改的不带。
- 问人的问题由代码生成时，human_input_required 的 question_code 与 Verdict 的 reason_code 相同；模型写的问题不带。
- 问人后停止或没回复，工作流推一个 abort 的 verdict 事件，reason_code 是 human_stopped、human_no_reply；before_finish
  驳回用满上限后的问人是 finish_rejected。
- Node 失败的说明写成“代码：说明”的，node_finished 带 error_code（budget_check 记为 budget_exhausted、budget 为 usd），
  上游没成功而跳过的是 upstream_failed；tool_loop 里工具的错误写成“代码: 说明”的，tool_status 带 error_code。
- 代码写的固定规划（fast、Recipe、保守规划）的 goal、exit_when 带 goal_code、exit_when_code 与 params（PlanRoundOutput，
  stage_started 带出），文字留着作后备；Planner 模型写的不带。原地重跑沿用这个 Stage 的代码，重新规划后换成新规划的。
"""

from __future__ import annotations

from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from agentruntime.common.v1 import enums_pb2

from agentruntime_worker.activities.types import PlanRoundOutput, StagePolicy, ToolLoopStep
from agentruntime_worker.planner.activities import PlannerActivities
from agentruntime_worker.planner.context import StaticContextSource
from agentruntime_worker.planner.planner import Planner
from agentruntime_worker.reviewer.rules import Caps
from agentruntime_worker.workflows.events import stage_payload
from agentruntime_worker.workflows.task import _finish_escalation

from .planner_support import CI_RECIPE, FakeGateway, context, last_round, plan_answer, signals
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


EXPLORE_CODES = {"goal_code": "explore_task", "exit_when_code": "node_result", "params": {"node": "explore"}}


def has_no_codes(event: dict[str, Any]) -> bool:
    return not set(EXPLORE_CODES) & set(event)


@marked
async def test_stage_started_carries_the_plans_codes_next_to_the_text(harness: StageHarness) -> None:
    harness.script.plan_codes[1] = EXPLORE_CODES
    one_round(harness, deliver("answer"))
    inp, _ = await harness.run()
    started = events(harness, inp.task_id, "stage_started")[0]
    assert {k: started[k] for k in EXPLORE_CODES} == EXPLORE_CODES
    assert started["goal"] == "第 1 轮" and started["exit_when"] == "完成", "文字留着作后备"
    assert_valid("task_event_payload", started)


@marked
async def test_a_rerun_keeps_the_stages_codes_and_a_new_plan_replaces_them(harness: StageHarness) -> None:
    """原地重跑不经 Planner，goal、exit_when 还是这个 Stage 的文字，代码也得跟着：否则英文界面重跑时又是中文。"""
    s = harness.script
    s.plans["main"] = [run_stage(i, deliver("answer")) for i in (1, 2, 3)]
    s.deliver["main"] = "answer"
    s.plan_codes[1] = EXPLORE_CODES
    s.reviewer = scripted(
        out("repair", "再试一次", repair={"nodes": ["answer"], "strategy": "retry_with_error", "guidance": "换个写法"}),
        out("continue", "下一轮回 Planner"),
        out("done", "成稿", done={"deliver_node": "answer"}),
    )
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE and len(s.plan_inputs) == 2, "第 2 轮原地重跑，第 3 轮回 Planner"
    _, rerun, replanned = events(harness, inp.task_id, "stage_started")
    assert "triage_level" not in rerun and {k: rerun[k] for k in EXPLORE_CODES} == EXPLORE_CODES
    assert replanned["triage_level"] == "full" and has_no_codes(replanned), "新规划没有代码时不留上一个的"
    for e in harness.script.progress(inp.task_id):
        assert_valid("task_event_payload", e)


def test_stage_payload_drops_a_malformed_code_but_keeps_the_text() -> None:
    stage_ = {"goal": "文字", "exit_when": "也是文字", "nodes": []}
    plan = PlanRoundOutput(stage=stage_, triage_level="fast", goal_code="Not A Code", exit_when_code="reply_given")
    payload = stage_payload(stage_, "fast", plan)
    assert "goal_code" not in payload and payload["exit_when_code"] == "reply_given"
    assert payload["goal"] == "文字" and payload["exit_when"] == "也是文字"


def test_stage_payload_has_no_params_without_a_code() -> None:
    stage_ = {"goal": "文字", "exit_when": "也是文字", "nodes": []}
    plan = PlanRoundOutput(stage=stage_, triage_level="full", params={"node": "explore"})
    assert "params" not in stage_payload(stage_, "full", plan)


# --- Planner：代码写的固定规划 ---


def plan_round(gateway: FakeGateway, ctx: Any = None) -> Any:
    return PlannerActivities(Planner(gateway), StaticContextSource(ctx or context())).plan_round


def coded(out_: PlanRoundOutput) -> tuple[str, str, Any]:
    """规划的代码与参数；顺带核对 stage_started 的载荷符合 schema，Stage 里的文字还在。"""
    assert out_.stage["goal"] and out_.stage["exit_when"], "文字留着作后备"
    assert_valid("task_event_payload", {"event": "stage_started", "round_seq": 1, **stage_payload(out_.stage, out_.triage_level, out_)})
    return out_.goal_code, out_.exit_when_code, out_.params


async def test_a_fast_plan_codes_its_goal_and_exit_condition() -> None:
    gateway = FakeGateway(triage=signals("follow_up", family=None, effect="none"))
    out_ = await ActivityEnvironment().run(plan_round(gateway), plan_input("再短一点"))
    assert out_.triage_level == "fast"
    assert coded(out_) == ("reply_to_message", "reply_given", None)


async def test_a_recipe_plan_codes_its_goal_and_exit_condition_with_the_recipe() -> None:
    gateway = FakeGateway(triage=signals("new_task", family="engineering.ci_fix", effect="write_reversible"))
    ctx = context(risk="write_reversible", ceiling="write_reversible", recipes=[CI_RECIPE], budget=5.0)
    out_ = await ActivityEnvironment().run(plan_round(gateway, ctx), plan_input("还按老办法修一下"))
    assert out_.triage_level == "light"
    assert coded(out_) == ("run_recipe", "recipe_delivered", {"recipe": CI_RECIPE.recipe})
    assert "还按老办法修一下" in out_.stage["goal"], "后备的文字仍是完整的说明"


async def test_a_conservative_plan_without_the_model_codes_its_goal_and_exit_condition() -> None:
    gateway = FakeGateway()
    out_ = await ActivityEnvironment().run(plan_round(gateway), plan_input(no_model=True))
    assert not gateway.requests
    assert coded(out_) == ("explore_task", "node_result", {"node": "explore"})


async def test_a_conservative_plan_after_two_bad_answers_is_coded_the_same_way() -> None:
    gateway = FakeGateway(triage=signals(confidence=0.6), plan=["这不是 JSON", {"stage": "也不对"}])
    out_ = await ActivityEnvironment().run(plan_round(gateway), plan_input())
    assert coded(out_) == ("explore_task", "node_result", {"node": "explore"})


async def test_the_exit_condition_names_the_node_the_conservative_plan_really_uses() -> None:
    """要保留的 Node 占了 explore 这个 id 时，保守规划换一个 id，参数跟着换。"""
    replan = last_round({"verdict": "replan", "reason": "假设被证伪", "replan": {"keep_nodes": ["explore"]}})
    out_ = await ActivityEnvironment().run(plan_round(FakeGateway()), plan_input(round_seq=2, last_round=replan, no_model=True))
    assert out_.stage["nodes"][0]["id"] == "explore_rest"
    assert coded(out_) == ("explore_task", "node_result", {"node": "explore_rest"})


async def test_a_plan_the_model_wrote_has_no_codes() -> None:
    gateway = FakeGateway(triage=signals(confidence=0.6), plan=[plan_answer()])
    out_ = await ActivityEnvironment().run(plan_round(gateway), plan_input())
    assert out_.triage_level == "full"
    assert coded(out_) == ("", "", None)
