"""S-10 验收：Task 工作流按 Verdict 的去向走（附录 B.4、B.8；设计书 4.11），汇总写进 Task State，收尾写回不阻塞交付（附录 B.9）。

Reviewer 由脚本给出 ReviewRoundOutput（Verdict 与要写进 Task State 的汇总）；最后一条换成真实的 Reviewer
（规则 + 永远原地重试的假模型）跑震荡场景。新的去向在版本分支后面：旧历史按旧路径回放（见 test_stage_replay）。

去向：
- continue、replan、repair 换方法、reroute、rollback、问人后继续或重规划 → 下一轮回 Planner，last_round 带上 Verdict（与人的回复）；
- repair 原地重试、escalate 换更强的 Tier → 下一轮不回 Planner，重跑同一个 Stage 里出问题的部分，其余成功的 Node 结果沿用；
- done、abort、问人后停止 → 收尾。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from google.protobuf import json_format
from temporalio.api.enums.v1 import EventType
from temporalio.exceptions import ApplicationError

from agentruntime.common.v1 import enums_pb2
from agentruntime.workflow.v1 import task_pb2

from agentruntime_worker import names
from agentruntime_worker.activities.types import HumanQuestion, NodeInput, ReviewRoundInput, ReviewRoundOutput, StagePolicy, ToolLoopStep
from agentruntime_worker.reviewer.activities import ReviewerActivities, StaticCatalogSource
from agentruntime_worker.reviewer.reviewer import Reviewer
from agentruntime_worker.reviewer.signals import error_signature

from .reviewer_support import CATALOG, HUMAN_REPLIES, FakeReviewGateway, always, retry
from .stage_harness import Script, StageHarness, fails, harness  # noqa: F401 — harness 是 fixture
from .support import assert_valid
from .test_stage_runner import atomic, deliver, goal, scripted_steps, stage

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

ESCALATION_BOUND = 12
"""各级次数上限之和加一（见 test_reviewer_oscillation）。"""


def out(verdict: str, reason: str = "脚本的判定", state_update: dict[str, Any] | None = None, **details: Any) -> ReviewRoundOutput:
    v: dict[str, Any] = {"verdict": verdict, "reason": reason, **details}
    return ReviewRoundOutput(verdict=v, state_update=state_update or {})


def scripted(*outputs: ReviewRoundOutput) -> Callable[[ReviewRoundInput], Awaitable[ReviewRoundOutput]]:
    queue = list(outputs)

    async def review(inp: ReviewRoundInput) -> ReviewRoundOutput:
        assert queue, f"脚本里没有第 {inp.round_seq} 轮的判定"
        return queue.pop(0)

    return review


def fail_once(message: str) -> Any:
    """第一次执行失败，之后成功（模拟原地重试能修好的执行层问题）。"""
    seen: set[str] = set()

    async def behave(inp: NodeInput, _: Script) -> dict[str, Any]:
        if inp.node["id"] not in seen:
            seen.add(inp.node["id"])
            raise ApplicationError(message, type="stub_failure", non_retryable=True)
        return {"result": {"node": inp.node["id"], "ok": True}, "meta": {"cost_usd": 0.01}}

    return behave


def costs(value: float, result: Any = None) -> Any:
    async def behave(inp: NodeInput, _: Script) -> dict[str, Any]:
        return {"result": result if result is not None else {"node": inp.node["id"]}, "rationale": "不往下传", "meta": {"cost_usd": value}}

    return behave


def executed_ids(s: Script, task_id: str) -> list[str]:
    return [c.node_id for c in s.executed(task_id)]


async def task_state(h: StageHarness, task_id: str) -> dict[str, Any]:
    q = await h.client.get_workflow_handle(task_id).query(names.TASK_STATE_QUERY, result_type=task_pb2.TaskStateQueryResult)
    return json_format.MessageToDict(q.task_state.value)


def deliverable(result: task_pb2.TaskWorkflowResult) -> Any:
    return json_format.MessageToDict(result.deliverable.value)


def reply_with(h: StageHarness, option: str, guidance: str = "") -> Callable[[HumanQuestion], Awaitable[None]]:
    async def answer(q: HumanQuestion) -> None:
        await h.client.get_workflow_handle(q.task_id).signal(
            names.HUMAN_REPLY_SIGNAL, task_pb2.HumanReplySignal(question_id=q.question_id, option=option, guidance=guidance, replied_by_user_id="u1")
        )

    return answer


FETCH_TWO = [
    atomic("q_a", role="fetch", effect="read"),
    atomic("q_b", role="fetch", effect="read"),
    deliver("report", after=["q_a", "q_b"], inputs={"a": "$q_a.result", "b": "$q_b.result"}),
]


# --- repair：原地重试与换方法 ---


async def test_retry_reruns_only_the_failed_part_of_the_stage_and_keeps_the_rest(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, *FETCH_TWO)]
    s.behaviors["q_b"] = fail_once("数据仓库限流（rate_limit）")
    s.reviewer = scripted(
        out("repair", "临时错误，重试即可", repair={"nodes": ["q_b"], "strategy": "retry_with_error", "guidance": "换个时间窗口"}),
        out("done", "成稿", done={"deliver_node": "report"}),
    )
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE and result.deliver_node_id == "report" and result.rounds == 2
    assert len(s.plan_inputs) == 1, "原地重试不回 Planner"
    assert sorted(executed_ids(s, inp.task_id)) == ["q_a", "q_b", "q_b", "report"], "q_a 的结果沿用，不重跑"
    rerun = [c for c in s.executed(inp.task_id) if c.node_id == "q_b"][1].inp
    injected = json.dumps(rerun.injections, ensure_ascii=False)
    assert "rate_limit" in injected and "换个时间窗口" in injected, "重试时带上错误与 Reviewer 的指导"
    report = next(c for c in s.executed(inp.task_id) if c.node_id == "report").inp
    assert report.node["inputs"]["a"]["node"] == "q_a", "沿用的结果仍能被下游引用"
    second = s.review(inp.task_id, 2)
    assert set(second.stage_results) == {"q_a", "q_b", "report"} and all(r.status == "succeeded" for r in second.stage_results.values())
    state = await task_state(harness, inp.task_id)
    assert [(r["seq"], r["stage_goal"], r["verdict"]) for r in state["rounds"]] == [(1, "第 1 轮", "repair"), (2, "第 1 轮", "done")]
    assert "triage_level" not in state["rounds"][1], "没有经过 Planner 的 Round 没有 Triage"


async def test_retry_of_a_goal_node_carries_the_error_and_guidance_into_its_goal(harness: StageHarness) -> None:
    s = harness.script
    calls = {"n": 0}

    def step(inp: Any) -> ToolLoopStep:
        # 第一次跑：一步就用完 max_iterations，Goal Node 失败；重跑时直接完成。
        calls["n"] += 1
        if calls["n"] == 1:
            return ToolLoopStep(action="tool", tool="repo_read", args={"path": "setup.cfg"})
        return ToolLoopStep(action="finish", result={"patch": "diff"})

    s.tool_steps["fix_lint"] = step
    s.plans["main"] = [stage(1, goal("fix_lint", "tool_loop", tools=["repo_read"], max_iterations=1), deliver(after=["fix_lint"]))]
    s.reviewer = scripted(
        out("repair", "换方法", repair={"nodes": ["fix_lint"], "strategy": "retry_with_error", "guidance": "改用格式化工具统一处理行宽"}),
        out("done", "lint 通过", done={"deliver_node": "answer"}),
    )
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    steps = [c.inp for c in s.calls if c.activity == names.TOOL_LOOP_STEP and c.workflow_id.startswith(inp.task_id)]
    assert "改用格式化工具统一处理行宽" in steps[-1].node["goal"], "Goal Node 重跑时，指导写进它的 goal"


async def test_change_method_goes_back_to_the_planner(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, *FETCH_TWO), stage(2, deliver("report2"))]
    s.behaviors["q_b"] = fails("口径对不上")
    verdict = {"nodes": ["q_b"], "strategy": "change_method", "guidance": "改用渠道明细表"}
    s.reviewer = scripted(out("repair", "换方法", repair=verdict), out("done", "完成", done={"deliver_node": "report2"}))
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert [p.round_seq for p in s.plan_inputs] == [1, 2]
    last = s.plan_inputs[1].last_round
    assert last["verdict"]["verdict"] == "repair" and last["verdict"]["repair"] == verdict


async def test_a_stronger_tier_reruns_the_failed_nodes_at_that_tier(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, atomic("prove", executor="model", template="answer@v1", tier="strong@medium"), deliver(after=["prove"]))]
    s.behaviors["prove"] = fail_once("第 3 步用到了待证结论")
    s.reviewer = scripted(
        out("escalate", "repair、换方法、replan 都试过", escalate={"target": "tier", "tier": "strong@high"}),
        out("done", "证明通过", done={"deliver_node": "answer"}),
    )
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE and len(s.plan_inputs) == 1
    proves = [c.inp for c in s.executed(inp.task_id) if c.node_id == "prove"]
    assert [p.node.get("tier") for p in proves] == ["strong@medium", "strong@high"]


# --- escalate：问人 ---


@pytest.mark.parametrize("option", HUMAN_REPLIES)
async def test_escalating_to_a_human_offers_three_replies_and_follows_the_answer(harness: StageHarness, option: str) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, atomic("compare", role="fetch", effect="read"), deliver("summary", after=["compare"])), stage(2, deliver("ticket"))]
    question = "只剩不可退改的 12,800 元票价。按此订、带指导重新安排，还是停止？"
    s.on_question = reply_with(harness, option, guidance="改订 3 号")
    s.reviewer = scripted(
        out("escalate", "高风险决定", escalate={"target": "human", "question": question, "options": list(HUMAN_REPLIES), "state_summary": "已比价，最便宜的不可退改"}),
        out("done", "出票成功", done={"deliver_node": "ticket"}),
    )
    inp, result = await harness.run()
    asked = [q for q in s.questions if q.task_id == inp.task_id]
    assert len(asked) == 1
    q = asked[0]
    assert q.question_id == "_review" and q.round_seq == 1, "工作流里的问题 id 不会与任何 Node id 冲突"
    assert q.options == HUMAN_REPLIES and q.text == question and q.state_summary == "已比价，最便宜的不可退改"
    if option == "stop_and_wrap_up":
        assert result.status == enums_pb2.TASK_STATUS_ABORTED and result.final_verdict == enums_pb2.VERDICT_ABORT
        assert len(s.plan_inputs) == 1
        body = deliverable(result)
        assert body["partial"] and result.deliver_node_id == "summary", "停止时用现有结果收尾"
        return
    assert result.status == enums_pb2.TASK_STATUS_DONE
    last = s.plan_inputs[1].last_round
    assert last["verdict"]["verdict"] == "escalate"
    assert last["human_reply"] == {"option": option, "guidance": "改订 3 号"}


async def test_an_unanswered_escalation_stops_and_wraps_up(harness: StageHarness) -> None:
    s = harness.script
    s.policy = StagePolicy(human_timeout_s=1)
    s.plans["main"] = [stage(1, atomic("compare", role="fetch", effect="read"))]
    s.reviewer = scripted(out("escalate", "要问人", escalate={"target": "human", "question": "继续吗？", "options": list(HUMAN_REPLIES), "state_summary": "x"}))
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_ABORTED and result.final_verdict == enums_pb2.VERDICT_ABORT
    assert len(s.plan_inputs) == 1
    assert s.withdrawn == ["_review"], "超时后撤回问题"


# --- rollback 与 reroute ---


async def test_rollback_restores_the_task_state_of_the_checkpoint(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, atomic("a")), stage(2, atomic("b")), stage(3, atomic("c")), stage(4, deliver("fixed"))]
    s.reviewer = scripted(
        out("continue", "第一轮完成", state_update={"facts": ["事实一"], "artifacts": ["obj://t1/x/v1.db"]}),
        out("continue", "第二轮完成", state_update={"facts": ["事实二"], "artifacts": ["obj://t1/x/v2.db"]}),
        out("rollback", "账户文件被清空", rollback={"checkpoint": "round_1"}),
        out("done", "修好了", done={"deliver_node": "fixed"}),
    )
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    fourth = s.plan_inputs[3]
    assert fourth.task_state["facts"] == ["事实一"] and fourth.task_state["artifacts"] == ["obj://t1/x/v1.db"]
    assert fourth.last_round["verdict"]["rollback"] == {"checkpoint": "round_1"}
    state = await task_state(harness, inp.task_id)
    assert [r["verdict"] for r in state["rounds"]] == ["continue", "continue", "rollback", "done"], "Round 记录本身不回退"


async def test_reroute_goes_back_to_the_planner_with_the_new_recipe(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, goal("explore", "tool_loop", tools=["repo_read"])), stage(2, deliver("done_node"))]
    s.tool_steps["explore"] = scripted_steps(ToolLoopStep(action="finish", result={"finding": "适合工单批处理流程"}))
    s.reviewer = scripted(out("reroute", "属于批量打标签流程", reroute={"recipe": "ticket_triage@v4"}), out("done", "完成", done={"deliver_node": "done_node"}))
    inp, result = await harness.run()
    second = s.plan_inputs[1]
    assert second.last_round["verdict"]["reroute"] == {"recipe": "ticket_triage@v4"}
    assert second.task_state["pinned"]["recipe"] == "ticket_triage@v4" and second.pinned["recipe"] == "ticket_triage@v4"


# --- done 与 abort ---


async def test_done_naming_a_node_that_did_not_deliver_fails_cleanly(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, *FETCH_TWO)]
    s.reviewer = scripted(out("done", "完成", done={"deliver_node": "q_a"}))
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_FAILED and result.error.code == "invalid_verdict"
    assert len([f for f in s.finalized if f.task_id == inp.task_id]) == 1


async def test_abort_delivers_partial_results_and_says_what_is_missing(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, *FETCH_TWO)]
    s.reviewer = scripted(out("abort", "预算用尽", abort={"completed": ["两组数据"], "not_completed": ["归因"]}))
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_ABORTED and result.final_verdict == enums_pb2.VERDICT_ABORT
    body = deliverable(result)
    assert body["completed"] == ["两组数据"] and body["not_completed"] == ["归因"] and body["reason"] == "预算用尽"
    assert body["partial"]["node"] == "report" and result.deliver_node_id == "report"


# --- 汇总与 Reviewer 的输入 ---


async def test_the_round_summary_updates_the_task_state(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, atomic("q_a", role="fetch", effect="read"), atomic("q_b", role="fetch", effect="read")), stage(2, deliver("report"))]
    s.behaviors["q_a"] = costs(0.12)
    s.behaviors["q_b"] = costs(0.08)
    s.behaviors["report"] = costs(0.05)
    update = {"facts": ["华东退款率明显高于其他区域"], "open_questions": ["物流商是否更换过"], "artifacts": ["obj://t1/x/region.csv"]}
    s.reviewer = scripted(out("continue", "取数完成", state_update=update), out("done", "完成", done={"deliver_node": "report"}))
    inp, result = await harness.run()
    state = await task_state(harness, inp.task_id)
    assert_valid("task_state", state)
    assert state["facts"] == ["华东退款率明显高于其他区域"] and state["open_questions"] == ["物流商是否更换过"]
    assert state["artifacts"] == ["obj://t1/x/region.csv"]
    assert [r["cost_usd"] for r in state["rounds"]] == pytest.approx([0.2, 0.05])
    assert list(result.artifacts) == ["obj://t1/x/region.csv"]
    assert s.review(inp.task_id, 1).task_state["rounds"][0]["cost_usd"] == pytest.approx(0.2), "Reviewer 看到本轮的成本"


async def test_the_reviewer_gets_the_stage_and_the_history_but_no_planning_output(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, *FETCH_TWO)]
    s.behaviors["q_b"] = fail_once("数据仓库限流（rate_limit）")
    s.reviewer = scripted(out("repair", "重试", repair={"nodes": ["q_b"], "strategy": "retry_with_error"}), out("done", "完成", done={"deliver_node": "report"}))
    inp, _ = await harness.run()
    first, second = s.review(inp.task_id, 1), s.review(inp.task_id, 2)
    assert first.history == [] and first.stage == s.plans["main"][0] | {"seq": 1}
    assert second.history == [{"round": 1, "verdict": "repair", "step": "repair_retry", "failed_nodes": {"q_b": error_signature("数据仓库限流（rate_limit）")}}]
    dumped = json.dumps(dataclasses.asdict(second), ensure_ascii=False)
    assert "rationale" not in dumped and "task_plan" not in dumped


# --- 收尾：先交付，写回不阻塞 ---


async def test_wrap_up_notifies_the_session_before_writing_back(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, deliver())]
    s.reviewer = scripted(out("done", "完成", done={"deliver_node": "answer"}))
    inp, session = await harness.start_via_session()
    notified = await asyncio.wait_for(session.result(), timeout=60)
    assert notified.status == enums_pb2.TASK_STATUS_DONE
    history = await harness.client.get_workflow_handle(inp.task_id).fetch_history()
    kinds = []
    for event in history.events:
        if event.event_type == EventType.EVENT_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED:
            kinds.append("notify")
        elif event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED and event.activity_task_scheduled_event_attributes.activity_type.name == names.FINALIZE_TASK:
            kinds.append("finalize")
    assert kinds == ["notify", "finalize"], "先把结果交给 Session，再写回"


async def test_a_failing_write_back_does_not_change_the_outcome(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, deliver())]
    s.finalize_error = "Memory Service 不可用"
    s.reviewer = scripted(out("done", "完成", done={"deliver_node": "answer"}))
    inp, session = await harness.start_via_session()
    notified = await asyncio.wait_for(session.result(), timeout=60)
    result = await asyncio.wait_for(harness.client.get_workflow_handle(inp.task_id, result_type=task_pb2.TaskWorkflowResult).result(), timeout=60)
    assert notified.status == result.status == enums_pb2.TASK_STATUS_DONE


# --- 震荡：真实的 Reviewer ---


async def test_a_task_that_keeps_failing_ends_within_the_escalation_caps(harness: StageHarness) -> None:
    """计划 S-10 验收“震荡测试”的端到端版本：Stage 永远失败，模型永远说原地重试；问人时人选带指导重规划。"""
    s = harness.script
    acts = ReviewerActivities(Reviewer(FakeReviewGateway(always(retry("ghost")))), StaticCatalogSource(CATALOG), None)
    s.reviewer = acts.review
    s.plan_for = lambda inp: stage(inp.round_seq, atomic(f"fetch_{inp.round_seq}", role="fetch", effect="read", template="fetch@v1"), deliver(after=[f"fetch_{inp.round_seq}"]))
    s.behaviors["fetch@v1"] = fails("数据源返回 500")
    s.on_question = reply_with(harness, "replan_with_guidance", guidance="换个数据源")
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_ABORTED and result.final_verdict == enums_pb2.VERDICT_ABORT
    assert result.rounds <= ESCALATION_BOUND, f"跑了 {result.rounds} 轮"
    assert len([r for r in s.reviews if r.task_id == inp.task_id]) == result.rounds
