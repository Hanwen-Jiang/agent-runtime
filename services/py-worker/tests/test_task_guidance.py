"""docs/plan/running-task-interaction.md 第 2 节验收：纠偏、立刻打断、撤回与修改（工作流这一层）。

用户在运行中发的消息可以选三种：纠偏（下一个检查点生效）、立刻打断（放弃正在进行的调用，马上生效）、下一个任务（排队，
现在的行为）。这里是前两种在 Task 工作流里的表现，第 2 节两张表的每一格一条：

- tool_loop 等模型、跑工具、等审批；Atomic Node、模板、sub_agent、子 Task；Planner 规划中；Reviewer 评审中；已结束。
- 撤销：模型调用取消、只读工具取消、write_reversible 取消后报告撤销的结果、write_irreversible 不打断。
- 撤回与修改：还没生效的可以；已生效的与立刻打断不行；谁的信号先到谁算。
- 取消整个 Task 优先于打断。纠偏写进 Task State，撤回的模型看不到；文字不进事件与日志。

工具真的被杀掉、文件真的还原见 test_interrupt_undo.py 与 test_workspace_checkpoint.py；API 与 Session 工作流见 go-task-service。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowFailureError

from agentruntime.common.v1 import enums_pb2
from agentruntime.workflow.v1 import task_pb2

from agentruntime_worker import names
from agentruntime_worker.activities.types import TemplateExpansion, ToolLoopStep
from agentruntime_worker.reviewer import prompts as reviewer_prompts
from agentruntime_worker.workflows import patches
from agentruntime_worker.workflows import stage_plan as sp
from agentruntime_worker.workflows import verdicts as vd

from .stage_harness import Hold, StageHarness, harness, held  # noqa: F401 — harness 是 fixture
from .support import assert_valid, task_input
from .test_parallel_tool_calls import FINISH, edit, paths, read, respond, script_loop
from .test_progress_events import progress
from .test_stage_runner import atomic, deliver, goal, one_round, stage, wait_at_gate
from .test_task_verdicts import out, scripted, task_state
from .test_tool_loop_approval import IRREVERSIBLE, loop_steps, reply, tool_calls, wait_for_question

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

NEXT_STEP = enums_pb2.GUIDANCE_MODE_NEXT_STEP
INTERRUPT = enums_pb2.GUIDANCE_MODE_INTERRUPT
HUMAN_REPLIES = ["continue_as_is", "replan_with_guidance", "stop_and_wrap_up"]
# “立刻”的上限：现在 Node 的 heartbeat 超时最长 30 秒，取消要 24 秒才送到，不算立刻。
PROMPTLY = 8.0


def signal(guidance_id: str, text: str = "", *, mode: int = NEXT_STEP, kind: int = task_pb2.TASK_GUIDANCE_KIND_SUBMIT) -> task_pb2.TaskGuidanceSignal:
    return task_pb2.TaskGuidanceSignal(guidance_id=guidance_id, kind=kind, text=text, mode=mode, by_user_id="u1")


async def steer(handle: Any, guidance_id: str, text: str) -> None:
    await handle.signal(names.TASK_GUIDANCE_SIGNAL, signal(guidance_id, text))


async def interrupt(handle: Any, guidance_id: str, text: str) -> None:
    await handle.signal(names.TASK_GUIDANCE_SIGNAL, signal(guidance_id, text, mode=INTERRUPT))


async def retract(handle: Any, guidance_id: str) -> None:
    await handle.signal(names.TASK_GUIDANCE_SIGNAL, signal(guidance_id, kind=task_pb2.TASK_GUIDANCE_KIND_RETRACT))


async def amend(handle: Any, guidance_id: str, text: str) -> None:
    await handle.signal(names.TASK_GUIDANCE_SIGNAL, signal(guidance_id, text, kind=task_pb2.TASK_GUIDANCE_KIND_EDIT))


def updates(h: StageHarness, task_id: str, guidance_id: str) -> list[dict[str, Any]]:
    return [e for e in progress(h, task_id) if e["event"] == "guidance_updated" and e["guidance_id"] == guidance_id]


def states(h: StageHarness, task_id: str, guidance_id: str) -> list[str]:
    return [e["state"] for e in updates(h, task_id, guidance_id)]


async def wait_for_state(h: StageHarness, task_id: str, guidance_id: str, state: str) -> dict[str, Any]:
    await h.wait_for(lambda: state in states(h, task_id, guidance_id), f"纠偏 {guidance_id} 变成 {state}")
    return next(e for e in updates(h, task_id, guidance_id) if e["state"] == state)


async def entered(h: StageHarness, hold: Hold) -> None:
    await h.wait_for(hold.entered.is_set, "activity 开始运行")


async def cancelled(h: StageHarness, hold: Hold) -> None:
    await h.wait_for(lambda: hold.cancelled, f"取消在 {PROMPTLY} 秒内送到 activity", timeout=PROMPTLY)


async def finish(handle: Any) -> task_pb2.TaskWorkflowResult:
    return await asyncio.wait_for(handle.result(), timeout=60)


def told_the_model(call: Any) -> list[str]:
    """这次模型调用的 history 里带的纠偏。"""
    return [e["user_guidance"] for e in call.inp.history if "user_guidance" in e]


def tool_entries(call: Any) -> list[dict[str, Any]]:
    return [e for e in call.inp.history if "tool" in e]


def plans_of(h: StageHarness, task_id: str) -> list[Any]:
    return [p for p in h.script.plan_inputs if p.task_id == task_id]


def reviews_of(h: StageHarness, task_id: str) -> list[Any]:
    return [r for r in h.script.reviews if r.task_id == task_id]


def verdict_events(h: StageHarness, task_id: str) -> list[dict[str, Any]]:
    return [e for e in progress(h, task_id) if e["event"] == "verdict"]


def two_rounds(h: StageHarness, *first: dict[str, Any]) -> None:
    """第 1 轮是 first，第 2 轮只交付：纠偏之后回 Planner 走的就是第 2 轮。"""
    h.script.plans["main"] = [stage(1, *first), stage(2, deliver())]
    h.script.deliver["main"] = "answer"


def charge() -> dict[str, Any]:
    return {"tool": "charge", "args": {"amount": 1}}


def script_charge(h: StageHarness, *steps: ToolLoopStep) -> None:
    h.script.tools["charge"] = "write_irreversible"
    script_loop(h, *steps, tools=("charge", "repo_read"), ceiling="write_irreversible")


def test_the_version_branch_is_appended_at_the_end_of_at_start() -> None:
    # 追加时在末尾，之后再追加的排在后面（docs/plan/feedback-2026-11-01.md 的 REFERENCES_PATCH）：位置不能变。
    at = patches.AT_START.index(patches.GUIDANCE_PATCH)
    assert patches.AT_START[at - 1 : at + 2] == (patches.CONTEXT_USAGE_PATCH, patches.GUIDANCE_PATCH, patches.INTERRUPT_WITHDRAW_PATCH)


# --- 第 2 节第一张表：纠偏（下一个检查点） ---


async def test_next_step_during_a_model_wait_runs_the_reads_but_not_the_writes(harness: StageHarness) -> None:
    """这一步回来后，只读的工具照跑；写的不执行（用户可能正是要拦它），下一次模型调用带上纠偏。"""
    s = harness.script
    hold = s.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    script_loop(harness, respond(read("a.py"), read("b.py"), edit("c.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await steer(handle, "g1", "不要改 c.py")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    hold.release.set()
    result = await finish(handle)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert hold.cancelled is False, "纠偏不取消正在进行的调用"
    assert sorted(paths(tool_calls(harness, inp.task_id, "repo_read"))) == ["a.py", "b.py"]
    assert tool_calls(harness, inp.task_id, "repo_edit") == [], "写的不执行"
    second = loop_steps(harness, inp.task_id)[1]
    assert told_the_model(second) == ["不要改 c.py"]
    held_back = tool_entries(second)[2]
    assert held_back["tool"] == "repo_edit" and held_back["skipped"] is True and "纠偏" in held_back["error"], "每个调用都要有结果，否则下一次模型调用会被拒"
    assert states(harness, inp.task_id, "g1") == ["pending", "applied"]
    assert updates(harness, inp.task_id, "g1")[-1]["round_seq"] == 1


async def test_next_step_holds_back_a_single_write_too(harness: StageHarness) -> None:
    """一次回答只有一个调用时走的是另一条路径，同样要拦。"""
    hold = harness.script.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    script_loop(harness, respond(edit("c.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await steer(handle, "g1", "先别改")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    hold.release.set()
    await finish(handle)
    assert tool_calls(harness, inp.task_id, "repo_edit") == []
    assert tool_entries(loop_steps(harness, inp.task_id)[1])[0]["skipped"] is True


async def test_next_step_skips_what_comes_after_the_write_it_holds_back(harness: StageHarness) -> None:
    """第 1 节的规则：写之后的调用是按写成功来写的，写没执行，它们也不执行。"""
    hold = harness.script.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    script_loop(harness, respond(read("a.py"), edit("b.py"), read("c.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await steer(handle, "g1", "先别改")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    hold.release.set()
    await finish(handle)
    assert paths(tool_calls(harness, inp.task_id, "repo_read")) == ["a.py"]
    entries = tool_entries(loop_steps(harness, inp.task_id)[1])
    assert [e["args"]["path"] for e in entries] == ["a.py", "b.py", "c.py"]
    assert "observation" in entries[0] and entries[1]["skipped"] and entries[2]["skipped"]


async def test_next_step_does_not_ask_approval_for_a_write_it_holds_back(harness: StageHarness) -> None:
    s = harness.script
    hold = s.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    script_charge(harness, respond(charge()), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await steer(handle, "g1", "先别付")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    hold.release.set()
    await finish(handle)
    assert [q for q in s.questions if q.task_id == inp.task_id] == []
    assert tool_calls(harness, inp.task_id, "charge") == []


async def test_next_step_during_reads_finishes_them_and_tells_the_next_call(harness: StageHarness) -> None:
    hold = harness.script.hold(f"{names.CALL_TOOL}:fix.2.2")
    script_loop(harness, respond(read("a.py"), read("b.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await steer(handle, "g1", "重点看 b.py")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    hold.release.set()
    await finish(handle)
    assert hold.cancelled is False
    second = loop_steps(harness, inp.task_id)[1]
    assert [("observation" in e) for e in tool_entries(second)] == [True, True]
    assert told_the_model(second) == ["重点看 b.py"]


async def test_next_step_during_a_write_finishes_it_but_starts_no_more_writes(harness: StageHarness) -> None:
    """已经开始的跑完，还没开始的写不执行（理由同等模型时：用户可能正是要拦它）。"""
    hold = harness.script.hold(f"{names.CALL_TOOL}:fix.2")
    script_loop(harness, respond(edit("a.py"), edit("b.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await steer(handle, "g1", "b.py 不用改")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    hold.release.set()
    await finish(handle)
    assert paths(tool_calls(harness, inp.task_id, "repo_edit")) == ["a.py"]
    entries = tool_entries(loop_steps(harness, inp.task_id)[1])
    assert "observation" in entries[0] and entries[1]["skipped"] is True


async def test_next_step_while_asking_for_approval_keeps_waiting_and_carries_it_after_the_reply(harness: StageHarness) -> None:
    s = harness.script
    script_charge(harness, respond(charge()), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await wait_for_question(harness, inp.task_id, "fix.2.1")
    await steer(handle, "g1", "付完记得写日志")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    assert s.withdrawn == [], "纠偏不撤回问题"
    await handle.signal(names.HUMAN_REPLY_SIGNAL, reply("fix.2.1", "approve"))
    await finish(handle)
    assert len(tool_calls(harness, inp.task_id, "charge")) == 1, "人批准了就照办"
    assert told_the_model(loop_steps(harness, inp.task_id)[1]) == ["付完记得写日志"]


async def test_next_step_while_the_reviewer_asks_carries_it_after_the_reply(harness: StageHarness) -> None:
    s = harness.script
    two_rounds(harness, deliver())
    s.reviewer = scripted(
        out("escalate", "要问人", escalate={"target": "human", "question": "继续吗？", "options": list(HUMAN_REPLIES), "state_summary": "x"}),
        out("done", "完成", done={"deliver_node": "answer"}),
    )
    inp, handle = await harness.start()
    await wait_for_question(harness, inp.task_id, vd.REVIEW_QUESTION_ID)
    await steer(handle, "g1", "换一种写法")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    assert s.withdrawn == []
    await handle.signal(names.HUMAN_REPLY_SIGNAL, reply(vd.REVIEW_QUESTION_ID, "continue_as_is"))
    await finish(handle)
    assert plans_of(harness, inp.task_id)[1].task_state["guidance"][0]["text"] == "换一种写法"


def node_of_kind(h: StageHarness, kind: str) -> tuple[dict[str, Any], Hold]:
    """一个正在跑的 Node（id 是 work），停在 hold 上。"""
    s = h.script
    hold = Hold()
    if kind == "atomic":
        s.behaviors["work"] = held(hold)
        return atomic("work"), hold
    if kind == "template":
        s.behaviors["work"] = held(hold)
        s.recipes["r@v1"] = TemplateExpansion(nodes=[atomic("inner")], output="inner")
        return goal("work", "template", recipe="r@v1"), hold
    if kind == "sub_agent":
        s.tools["web_search"] = "read"
        s.holds[f"{names.TOOL_LOOP_STEP}:work.1"] = hold
        s.tool_steps["work"] = lambda _: ToolLoopStep(action="finish", result={"found": True})
        return goal("work", "sub_agent", tools=["web_search"]), hold
    assert kind == "sub_task"
    s.behaviors["child_answer"] = held(hold)
    s.plans["child"] = [stage(1, deliver("child_answer"))]
    s.deliver["child"] = "child_answer"
    return goal("work", "sub_task", inputs={"scenario": "child"}), hold


@pytest.mark.parametrize("kind", ["atomic", "template", "sub_agent", "sub_task"])
async def test_next_step_during_a_stage_replans_after_it(harness: StageHarness, kind: str) -> None:
    """这个 Stage 跑完回 Planner，以 replan 重做剩余部分。判定由代码给出，不花一次 Reviewer 的模型调用。"""
    node, hold = node_of_kind(harness, kind)
    two_rounds(harness, node, deliver(after=["work"]))
    inp, handle = await harness.start()
    await entered(harness, hold)
    await steer(handle, "g1", "报告里加上华东")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    hold.release.set()
    result = await finish(handle)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert hold.cancelled is False
    assert [r.round_seq for r in reviews_of(harness, inp.task_id)] == [2], "第 1 轮不调 Reviewer"
    second = plans_of(harness, inp.task_id)[1]
    verdict = second.last_round["verdict"]
    assert verdict["verdict"] == "replan" and verdict["reason_code"] == "user_guidance"
    assert {"work", "answer"} <= set(verdict["replan"]["keep_nodes"]), "已成功的 Node 不重做"
    assert second.task_state["guidance"] == [{"guidance_id": "g1", "text": "报告里加上华东", "mode": "next_step", "state": "applied", "applied_round": 1}]
    assert verdict_events(harness, inp.task_id)[0]["reason_code"] == "user_guidance"
    assert states(harness, inp.task_id, "g1") == ["pending", "applied"]
    state = await task_state(harness, inp.task_id)
    assert_valid("task_state", state)
    assert [r["verdict"] for r in state["rounds"]] == ["replan", "done"]


async def test_guidance_taken_by_a_tool_loop_does_not_replan(harness: StageHarness) -> None:
    """循环已经带上了纠偏，这个 Stage 照常结束、走 Reviewer；Reviewer 看得到纠偏，据此验收。"""
    hold = harness.script.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    script_loop(harness, respond(read("a.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await steer(handle, "g1", "只看 a.py")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    hold.release.set()
    await finish(handle)
    assert len(plans_of(harness, inp.task_id)) == 1
    [review] = reviews_of(harness, inp.task_id)
    assert review.task_state["guidance"][0]["text"] == "只看 a.py"
    assert review.task_state["guidance"][0]["state"] == "applied"


async def test_every_running_tool_loop_gets_the_guidance(harness: StageHarness) -> None:
    s = harness.script
    s.tools["repo_read"] = "read"
    holds = [s.hold(f"{names.TOOL_LOOP_STEP}:{n}.1") for n in ("left", "right")]
    for n in ("left", "right"):
        s.tool_steps[n] = lambda inp: respond(read("a.py")) if inp.iteration == 1 else FINISH
    one_round(harness, goal("left", "tool_loop", tools=["repo_read"]), goal("right", "tool_loop", tools=["repo_read"]), deliver(after=["left", "right"]))
    inp, handle = await harness.start()
    for hold in holds:
        await entered(harness, hold)
    await steer(handle, "g1", "只看 a.py")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    for hold in holds:
        hold.release.set()
    await finish(handle)
    for n in ("left", "right"):
        second = next(c for c in loop_steps(harness, inp.task_id) if c.node_id == f"{n}.3")
        assert told_the_model(second) == ["只看 a.py"], n
    assert states(harness, inp.task_id, "g1") == ["pending", "applied"], "只生效一次"


async def test_a_loop_that_starts_after_the_guidance_sees_it_in_task_state(harness: StageHarness) -> None:
    s = harness.script
    s.tools["repo_read"] = "read"
    hold = s.hold(f"{names.TOOL_LOOP_STEP}:first.1")
    s.tool_steps["first"] = lambda inp: respond(read("a.py")) if inp.iteration == 1 else FINISH
    s.tool_steps["later"] = lambda _: FINISH
    one_round(harness, goal("first", "tool_loop", tools=["repo_read"]), goal("later", "tool_loop", tools=["repo_read"], after=["first"]), deliver(after=["later"]))
    inp, handle = await harness.start()
    await entered(harness, hold)
    await steer(handle, "g1", "只看 a.py")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    hold.release.set()
    await finish(handle)
    later = next(c for c in loop_steps(harness, inp.task_id) if c.node_id == "later.1")
    assert later.inp.task_state["guidance"][0]["text"] == "只看 a.py"


async def test_a_sub_agent_does_not_see_the_guidance(harness: StageHarness) -> None:
    """sub_agent 是独立上下文（不带 Task State）。纠偏由下一轮 Planner 处理，不塞进它的上下文。"""
    node, hold = node_of_kind(harness, "sub_agent")
    two_rounds(harness, node, deliver(after=["work"]))
    inp, handle = await harness.start()
    await entered(harness, hold)
    await steer(handle, "g1", "换个方向")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    hold.release.set()
    await finish(handle)
    step = next(c for c in harness.script.calls if c.activity == names.TOOL_LOOP_STEP and c.node_id == "work.1")
    assert step.inp.task_state == {} and told_the_model(step) == []


async def test_next_step_while_planning_drops_the_stage_and_plans_again(harness: StageHarness) -> None:
    """规划完放弃这个 Stage，带上纠偏重新规划。放弃的规划也花了钱，算进这一轮。"""
    s = harness.script
    s.plan_cost = 0.1
    hold = s.hold(f"{names.PLAN_ROUND}:1")
    s.plans["main"] = [stage(1, atomic("work"), deliver(after=["work"]))]
    s.deliver["main"] = "answer"
    inp, handle = await harness.start()
    await entered(harness, hold)
    await steer(handle, "g1", "用另一种办法")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    hold.release.set()
    result = await finish(handle)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert hold.cancelled is False
    plans = plans_of(harness, inp.task_id)
    assert [p.round_seq for p in plans] == [1, 1], "还是同一轮"
    assert "guidance" not in plans[0].task_state or plans[0].task_state["guidance"] == []
    assert plans[1].task_state["guidance"][0]["text"] == "用另一种办法"
    events = progress(harness, inp.task_id)
    assert [e["event"] for e in events].count("stage_started") == 1, "放弃的 Stage 没有开始过"
    assert [e["event"] for e in events].count("round_started") == 1
    assert [c.node_id for c in s.executed(inp.task_id)].count("work") == 1
    state = await task_state(harness, inp.task_id)
    assert state["rounds"][0]["cost_usd"] == pytest.approx(0.2)


@pytest.mark.parametrize(
    "said",
    [
        out("done", "完成", done={"deliver_node": "answer"}),
        out("abort", "放弃", abort={"completed": [], "not_completed": ["全部"]}),
        out("continue", "继续"),
    ],
    ids=["done", "abort", "continue"],
)
async def test_next_step_while_reviewing_goes_back_to_the_planner_whatever_it_said(harness: StageHarness, said: Any) -> None:
    """不管判了什么（含 done）都不收尾，回 Planner。"""
    s = harness.script
    hold = s.hold(f"{names.REVIEW_ROUND}:1")
    two_rounds(harness, deliver())
    s.reviewer = scripted(said, out("done", "完成", done={"deliver_node": "answer"}))
    inp, handle = await harness.start()
    await entered(harness, hold)
    await steer(handle, "g1", "再加一节")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    hold.release.set()
    result = await finish(handle)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    plans = plans_of(harness, inp.task_id)
    assert len(plans) == 2
    verdict = plans[1].last_round["verdict"]
    assert verdict["verdict"] == "replan" and verdict["reason_code"] == "user_guidance"
    assert plans[1].task_state["guidance"][0]["text"] == "再加一节"
    assert [e["verdict"] for e in verdict_events(harness, inp.task_id)] == ["replan", "done"], "客户端看到的是实际的去向"
    assert len([f for f in s.finalized if f.task_id == inp.task_id]) == 1


async def test_guidance_during_wrap_up_is_rejected(harness: StageHarness) -> None:
    """已经在收尾：拒绝，界面提示可以作为新任务发送。"""
    s = harness.script
    one_round(harness, deliver())
    inp, handle = await harness.start()
    hold = s.hold(f"{names.FINALIZE_TASK}:{inp.task_id}")
    await entered(harness, hold)
    await steer(handle, "late", "来晚了")
    rejected = await wait_for_state(harness, inp.task_id, "late", "rejected")
    assert rejected["reason_code"] == "task_finished"
    hold.release.set()
    await finish(handle)


async def test_guidance_that_arrives_before_the_first_plan_is_not_lost(harness: StageHarness) -> None:
    """信号可以先于工作流的第一步到达。"""
    s = harness.script
    one_round(harness, deliver())
    inp = task_input(scenario="main")
    handle = await harness.client.start_workflow(
        names.TASK_WORKFLOW,
        inp,
        id=inp.task_id,
        task_queue=names.TASK_QUEUE,
        result_type=task_pb2.TaskWorkflowResult,
        start_signal=names.TASK_GUIDANCE_SIGNAL,
        start_signal_args=[signal("g0", "开头就有的要求")],
    )
    harness.handles.append(handle)
    await finish(handle)
    assert plans_of(harness, inp.task_id)[0].task_state["guidance"][0]["text"] == "开头就有的要求"
    assert s.plan_inputs


# --- 第 2 节第一张表与第二张表：立刻打断 ---


async def test_interrupt_during_a_model_wait_cancels_it_and_the_loop_goes_on(harness: StageHarness) -> None:
    """取消这次模型调用，丢弃未完成的输出；下一步带上纠偏，同一个循环继续，上下文不丢。"""
    s = harness.script
    hold = s.hold(f"{names.TOOL_LOOP_STEP}:fix.3")
    script_loop(harness, respond(read("a.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await interrupt(handle, "g1", "停，换个文件")
    await cancelled(harness, hold)
    result = await finish(handle)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    steps = loop_steps(harness, inp.task_id)
    assert [(c.node_id, c.outcome) for c in steps] == [("fix.1", "ok"), ("fix.3", "cancelled"), ("fix.3", "ok")]
    again = steps[2]
    assert again.inp.iteration == 2, "模型没有回答，这一步还是同一步"
    assert told_the_model(again) == ["停，换个文件"]
    assert tool_entries(again)[0]["args"]["path"] == "a.py", "之前的上下文还在"
    assert len(plans_of(harness, inp.task_id)) == 1, "不回 Planner"
    assert states(harness, inp.task_id, "g1") == ["pending", "applied"]
    assert "decision_retry" not in [e["event"] for e in progress(harness, inp.task_id)], "打断不是模型调用失败"


async def test_an_interrupted_model_call_does_not_use_up_an_iteration(harness: StageHarness) -> None:
    s = harness.script
    s.tools["repo_read"] = "read"
    s.tool_results["repo_read"] = lambda i: {"content": i.args["path"]}
    hold = s.hold(f"{names.TOOL_LOOP_STEP}:fix.3")
    s.tool_steps["fix"] = lambda inp: respond(read("a.py")) if inp.iteration == 1 else FINISH
    one_round(harness, goal("fix", "tool_loop", tools=["repo_read"], max_iterations=2), deliver(after=["fix"]))
    inp, handle = await harness.start()
    await entered(harness, hold)
    await interrupt(handle, "g1", "换个文件")
    await cancelled(harness, hold)
    result = await finish(handle)
    assert result.status == enums_pb2.TASK_STATUS_DONE, "max_iterations 是 2：被打断的那次不算"


async def test_interrupt_during_reads_cancels_them_and_every_call_has_a_result(harness: StageHarness) -> None:
    """已经有结果的留着；被取消的写明“被用户打断，没有结果”。每个调用都要有结果，否则下一次模型调用会被拒。"""
    s = harness.script
    hold_b = s.hold(f"{names.CALL_TOOL}:fix.2.2")
    hold_c = s.hold(f"{names.CALL_TOOL}:fix.2.3")
    script_loop(harness, respond(read("a.py"), read("b.py"), read("c.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold_b)
    await entered(harness, hold_c)
    await interrupt(handle, "g1", "不用读了")
    await cancelled(harness, hold_b)
    await cancelled(harness, hold_c)
    await finish(handle)
    second = loop_steps(harness, inp.task_id)[1]
    a, b, c = tool_entries(second)
    assert "observation" in a
    for entry in (b, c):
        assert entry["interrupted"] is True and "被用户打断，没有结果" in entry["error"]
    assert told_the_model(second) == ["不用读了"]


def tool_interrupted(undo: str) -> BaseException:
    """call_tool 被取消后报告撤销的结果（真实的实现见 test_interrupt_undo.py）。"""
    from agentruntime_worker.workflows.interrupts import tool_interrupted as make

    return make(undo)


@pytest.mark.parametrize(
    ("undo", "said"),
    [("restored", "已撤销这次调用的改动"), ("failed", "没能撤销"), ("no_checkpoint", "没能撤销")],
)
async def test_interrupt_during_a_reversible_write_reports_the_undo_and_does_not_retry(harness: StageHarness, undo: str, said: str) -> None:
    """撤销由 activity 做；工作流把结果照实告诉模型，被打断的写调用不重试。"""
    s = harness.script
    hold = s.hold(f"{names.CALL_TOOL}:fix.2", on_cancel=lambda: tool_interrupted(undo))
    script_loop(harness, respond(edit("a.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await interrupt(handle, "g1", "别改了")
    await cancelled(harness, hold)
    await finish(handle)
    calls = tool_calls(harness, inp.task_id, "repo_edit")
    assert len(calls) == 1 and calls[0].outcome == "cancelled", "不重试"
    second = loop_steps(harness, inp.task_id)[1]
    [entry] = tool_entries(second)
    assert entry["interrupted"] is True and said in entry["error"]
    assert told_the_model(second) == ["别改了"]


def test_interrupted_calls_are_not_repeated_errors() -> None:
    entries = [{"tool": "repo_edit", "args": {}, "error": "被用户打断，没有结果", "interrupted": True} for _ in range(sp.REPEATED_ERROR_LIMIT)]
    assert sp.repeated_error(entries) is False


async def test_interrupt_does_not_cancel_an_irreversible_write(harness: StageHarness) -> None:
    """做到一半撤不回：等它结束，之后立刻生效；界面说明“这一步不可撤销，完成后立刻生效”。"""
    s = harness.script
    hold = s.hold(f"{names.CALL_TOOL}:fix.2")
    script_charge(harness, respond(charge()), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await wait_for_question(harness, inp.task_id, "fix.2.1")
    await handle.signal(names.HUMAN_REPLY_SIGNAL, reply("fix.2.1", "approve"))
    await entered(harness, hold)
    await interrupt(handle, "g1", "别付了")
    pending = await wait_for_state(harness, inp.task_id, "g1", "pending")
    await harness.wait_for(lambda: "irreversible_in_flight" in [e.get("reason_code") for e in updates(harness, inp.task_id, "g1")], "说明要等不可逆的调用结束")
    assert pending["mode"] == "interrupt"
    await asyncio.sleep(1.5)
    assert hold.cancelled is False, "write_irreversible 不被打断"
    hold.release.set()
    await finish(handle)
    [call] = tool_calls(harness, inp.task_id, "charge")
    assert call.outcome == "ok"
    second = loop_steps(harness, inp.task_id)[1]
    assert "observation" in tool_entries(second)[0], "结果照实写进 history"
    assert told_the_model(second) == ["别付了"]


async def test_interrupt_while_asking_for_approval_withdraws_it_as_a_rejection(harness: StageHarness) -> None:
    s = harness.script
    script_charge(harness, respond(charge()), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await wait_for_question(harness, inp.task_id, "fix.2.1")
    await interrupt(handle, "g1", "不付了")
    await harness.wait_for(lambda: "fix.2.1" in s.withdrawn, "撤回审批问题")
    await handle.signal(names.HUMAN_REPLY_SIGNAL, reply("fix.2.1", "approve"))
    await finish(handle)
    assert tool_calls(harness, inp.task_id, "charge") == [], "撤回之后到的批准不算"
    second = loop_steps(harness, inp.task_id)[1]
    assert tool_entries(second)[0]["rejected"] is True
    assert told_the_model(second) == ["不付了"]


async def test_interrupt_while_the_reviewer_asks_withdraws_it_and_goes_to_the_planner(harness: StageHarness) -> None:
    s = harness.script
    two_rounds(harness, deliver())
    s.reviewer = scripted(
        out("escalate", "要问人", escalate={"target": "human", "question": "继续吗？", "options": list(HUMAN_REPLIES), "state_summary": "x"}),
        out("done", "完成", done={"deliver_node": "answer"}),
    )
    inp, handle = await harness.start()
    await wait_for_question(harness, inp.task_id, vd.REVIEW_QUESTION_ID)
    await interrupt(handle, "g1", "不用问了，按第二种")
    await harness.wait_for(lambda: vd.REVIEW_QUESTION_ID in s.withdrawn, "撤回问题")
    await finish(handle)
    second = plans_of(harness, inp.task_id)[1]
    assert second.last_round["verdict"]["reason_code"] == "user_interrupt"
    assert second.task_state["guidance"][0]["text"] == "不用问了，按第二种"


@pytest.mark.parametrize("kind", ["atomic", "template", "sub_agent", "sub_task"])
async def test_interrupt_during_a_stage_cancels_it_keeps_what_succeeded_and_replans(harness: StageHarness, kind: str) -> None:
    """取消这个 Stage，已成功的 Node 保留，以 replan 继续（reason_code 是 user_interrupt）。默认的 Node 也要很快送到取消。"""
    s = harness.script
    node, hold = node_of_kind(harness, kind)
    two_rounds(harness, atomic("first"), {**node, "after": ["first"]}, deliver(after=["work"]))
    inp, handle = await harness.start()
    await entered(harness, hold)
    await interrupt(handle, "g1", "停，先看另一个问题")
    await cancelled(harness, hold)
    result = await finish(handle)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert [r.round_seq for r in reviews_of(harness, inp.task_id)] == [2], "被打断的这一轮不调 Reviewer"
    second = plans_of(harness, inp.task_id)[1]
    verdict = second.last_round["verdict"]
    assert verdict["verdict"] == "replan" and verdict["reason_code"] == "user_interrupt"
    assert "first" in verdict["replan"]["keep_nodes"] and "work" not in verdict["replan"]["keep_nodes"]
    assert second.last_round["results"]["work"]["status"] != "succeeded"
    assert second.task_state["guidance"][0]["text"] == "停，先看另一个问题"
    assert second.task_state["guidance"][0]["applied_round"] == 1
    assert_valid("task_state", await task_state(harness, inp.task_id))


async def test_interrupt_stops_every_running_loop_in_the_stage(harness: StageHarness) -> None:
    s = harness.script
    s.tools["repo_read"] = "read"
    holds = [s.hold(f"{names.TOOL_LOOP_STEP}:{n}.1") for n in ("left", "right")]
    for n in ("left", "right"):
        s.tool_steps[n] = lambda _: FINISH
    one_round(harness, goal("left", "tool_loop", tools=["repo_read"]), goal("right", "tool_loop", tools=["repo_read"]), deliver(after=["left", "right"]))
    inp, handle = await harness.start()
    for hold in holds:
        await entered(harness, hold)
    await interrupt(handle, "g1", "都停一下")
    for hold in holds:
        await cancelled(harness, hold)
    await finish(handle)
    for n in ("left", "right"):
        again = [c for c in loop_steps(harness, inp.task_id) if c.node_id == f"{n}.1"][-1]
        assert told_the_model(again) == ["都停一下"], n


async def test_interrupt_waits_for_an_irreversible_node_and_cancels_the_rest(harness: StageHarness) -> None:
    s = harness.script
    pay = Hold()
    other = Hold()
    s.behaviors["pay"] = held(pay)
    s.behaviors["other"] = held(other)
    two_rounds(harness, atomic("pay", effect="write_irreversible"), atomic("other"), deliver(after=["pay", "other"]))
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await wait_at_gate(harness, inp.task_id, "pay")
    await handle.signal(names.HUMAN_REPLY_SIGNAL, task_pb2.HumanReplySignal(question_id="pay.1", option="approve", replied_by_user_id="u7"))
    await entered(harness, pay)
    await entered(harness, other)
    await interrupt(handle, "g1", "停")
    await cancelled(harness, other)
    await harness.wait_for(lambda: "irreversible_in_flight" in [e.get("reason_code") for e in updates(harness, inp.task_id, "g1")], "说明要等不可逆的 Node 结束")
    assert pay.cancelled is False
    pay.release.set()
    result = await finish(handle)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert pay.cancelled is False
    assert "pay" in plans_of(harness, inp.task_id)[1].last_round["verdict"]["replan"]["keep_nodes"]


async def test_interrupt_while_planning_cancels_it_and_plans_again(harness: StageHarness) -> None:
    s = harness.script
    hold = s.hold(f"{names.PLAN_ROUND}:1")
    one_round(harness, deliver())
    inp, handle = await harness.start()
    await entered(harness, hold)
    await interrupt(handle, "g1", "换个思路")
    await cancelled(harness, hold)
    result = await finish(handle)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    plans = plans_of(harness, inp.task_id)
    assert [p.round_seq for p in plans] == [1, 1]
    assert plans[1].no_model is False, "不是模型失败，不回退到不调模型的规划"
    assert plans[1].task_state["guidance"][0]["text"] == "换个思路"
    assert "decision_retry" not in [e["event"] for e in progress(harness, inp.task_id)]


async def test_interrupt_while_reviewing_cancels_it_and_goes_to_the_planner(harness: StageHarness) -> None:
    s = harness.script
    hold = s.hold(f"{names.REVIEW_ROUND}:1")
    two_rounds(harness, deliver())
    s.reviewer = scripted(out("done", "完成", done={"deliver_node": "answer"}))
    inp, handle = await harness.start()
    await entered(harness, hold)
    await interrupt(handle, "g1", "还没完")
    await cancelled(harness, hold)
    result = await finish(handle)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    reviews = reviews_of(harness, inp.task_id)
    assert [r.rules_only for r in reviews] == [False, False], "不是模型失败，不回退到只按规则判定"
    second = plans_of(harness, inp.task_id)[1]
    assert second.last_round["verdict"]["reason_code"] == "user_interrupt"
    assert second.task_state["guidance"][0]["text"] == "还没完"


async def test_interrupt_right_after_the_task_finished_is_rejected(harness: StageHarness) -> None:
    s = harness.script
    one_round(harness, deliver())
    inp, handle = await harness.start()
    hold = s.hold(f"{names.FINALIZE_TASK}:{inp.task_id}")
    await entered(harness, hold)
    await interrupt(handle, "late", "来晚了")
    assert (await wait_for_state(harness, inp.task_id, "late", "rejected"))["reason_code"] == "task_finished"
    assert hold.cancelled is False, "收尾不可打断"
    hold.release.set()
    await finish(handle)


# --- 打断与取消整个 Task ---


async def test_activities_that_wait_for_a_model_send_heartbeats_with_a_short_timeout(harness: StageHarness) -> None:
    """取消靠心跳送达：plan_round、review_round、tool_loop_step 都要有 heartbeat 超时，而且短到“立刻”算数。"""
    s = harness.script
    script_loop(harness, respond(read("a.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await finish(handle)
    history = await handle.fetch_history()
    seen: dict[str, float] = {}
    for event in history.events:
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            attrs = event.activity_task_scheduled_event_attributes
            if attrs.activity_type.name in (names.PLAN_ROUND, names.REVIEW_ROUND, names.TOOL_LOOP_STEP):
                seen[attrs.activity_type.name] = attrs.heartbeat_timeout.ToTimedelta().total_seconds()
    assert set(seen) == {names.PLAN_ROUND, names.REVIEW_ROUND, names.TOOL_LOOP_STEP}
    for name, timeout in seen.items():
        assert 0 < timeout * 0.8 <= PROMPTLY, f"{name} 的 heartbeat 超时是 {timeout} 秒"


async def test_cancelling_the_task_wins_over_an_interrupt(harness: StageHarness) -> None:
    """都表现为 CancelledError，要用标记区分。整个 Task 的取消优先：不能被当成打断、接着跑。"""
    s = harness.script
    hold = s.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    script_loop(harness, respond(read("a.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await interrupt(handle, "g1", "停一下")
    await handle.cancel()
    with pytest.raises(WorkflowFailureError):
        await finish(handle)
    q = await handle.query(names.TASK_STATE_QUERY, result_type=task_pb2.TaskStateQueryResult)
    assert q.status == enums_pb2.TASK_STATUS_CANCELLED
    assert len([f for f in s.finalized if f.task_id == inp.task_id]) == 1


async def test_an_interrupt_after_the_task_was_cancelled_changes_nothing(harness: StageHarness) -> None:
    s = harness.script
    hold = s.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    script_loop(harness, respond(read("a.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await handle.cancel()
    await interrupt(handle, "g1", "停一下")
    with pytest.raises(WorkflowFailureError):
        await finish(handle)
    q = await handle.query(names.TASK_STATE_QUERY, result_type=task_pb2.TaskStateQueryResult)
    assert q.status == enums_pb2.TASK_STATUS_CANCELLED


# --- 撤回与修改 ---


async def test_retract_before_it_takes_effect_leaves_no_trace(harness: StageHarness) -> None:
    """写照常执行，模型看不到，Task State 里也没有。"""
    hold = harness.script.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    script_loop(harness, respond(edit("a.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await steer(handle, "g1", "先别改")
    await retract(handle, "g1")
    await wait_for_state(harness, inp.task_id, "g1", "retracted")
    hold.release.set()
    await finish(handle)
    assert paths(tool_calls(harness, inp.task_id, "repo_edit")) == ["a.py"]
    second = loop_steps(harness, inp.task_id)[1]
    assert told_the_model(second) == []
    assert second.inp.task_state.get("guidance", []) == []
    assert (await task_state(harness, inp.task_id)).get("guidance", []) == []
    assert states(harness, inp.task_id, "g1") == ["pending", "retracted"]


async def test_a_retracted_guidance_does_not_replan_the_stage(harness: StageHarness) -> None:
    node, hold = node_of_kind(harness, "atomic")
    one_round(harness, node, deliver(after=["work"]))
    inp, handle = await harness.start()
    await entered(harness, hold)
    await steer(handle, "g1", "算了")
    await retract(handle, "g1")
    await wait_for_state(harness, inp.task_id, "g1", "retracted")
    hold.release.set()
    await finish(handle)
    assert len(plans_of(harness, inp.task_id)) == 1
    assert [r.round_seq for r in reviews_of(harness, inp.task_id)] == [1], "走 Reviewer"


async def test_retract_after_it_took_effect_is_rejected(harness: StageHarness) -> None:
    """工作流是单线程的：谁的信号先到谁算。已生效的留在 Task State 里，可以再发一条。"""
    s = harness.script
    hold = s.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    second_step = s.hold(f"{names.TOOL_LOOP_STEP}:fix.3")
    script_loop(harness, respond(read("a.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await steer(handle, "g1", "只看 a.py")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    hold.release.set()
    await entered(harness, second_step)
    await wait_for_state(harness, inp.task_id, "g1", "applied")
    await retract(handle, "g1")
    rejected = await wait_for_state(harness, inp.task_id, "g1", "rejected")
    assert rejected["reason_code"] == "already_applied"
    second_step.release.set()
    await finish(handle)
    assert (await task_state(harness, inp.task_id))["guidance"][0]["state"] == "applied"


async def test_edit_before_it_takes_effect_replaces_the_text(harness: StageHarness) -> None:
    hold = harness.script.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    script_loop(harness, respond(read("a.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await steer(handle, "g1", "只看 a.py")
    await amend(handle, "g1", "只看 b.py")
    await harness.wait_for(lambda: states(harness, inp.task_id, "g1") == ["pending", "pending"], "修改后又推一次 pending")
    hold.release.set()
    await finish(handle)
    assert told_the_model(loop_steps(harness, inp.task_id)[1]) == ["只看 b.py"]
    assert states(harness, inp.task_id, "g1") == ["pending", "pending", "applied"]


async def test_edit_after_it_took_effect_is_rejected(harness: StageHarness) -> None:
    s = harness.script
    hold = s.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    second_step = s.hold(f"{names.TOOL_LOOP_STEP}:fix.3")
    script_loop(harness, respond(read("a.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await steer(handle, "g1", "只看 a.py")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    hold.release.set()
    await entered(harness, second_step)
    await wait_for_state(harness, inp.task_id, "g1", "applied")
    await amend(handle, "g1", "改主意了")
    assert (await wait_for_state(harness, inp.task_id, "g1", "rejected"))["reason_code"] == "already_applied"
    second_step.release.set()
    await finish(handle)
    assert (await task_state(harness, inp.task_id))["guidance"][0]["text"] == "只看 a.py"


@pytest.mark.parametrize("act", [retract, lambda handle, gid: amend(handle, gid, "改主意了")], ids=["retract", "edit"])
async def test_an_interrupt_cannot_be_retracted_or_edited(harness: StageHarness, act: Any) -> None:
    """发出去调用就取消了。"""
    s = harness.script
    hold = s.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    script_loop(harness, FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    # 被打断的模型调用不等取消送到就重来，这里停住重来的那次，Task 才不会先结束。
    again = s.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    await interrupt(handle, "g1", "停")
    await entered(harness, again)
    await act(handle, "g1")
    assert (await wait_for_state(harness, inp.task_id, "g1", "rejected"))["reason_code"] == "already_applied"
    again.release.set()
    await finish(handle)
    assert (await task_state(harness, inp.task_id))["guidance"][0]["text"] == "停"


@pytest.mark.parametrize("act", [retract, lambda handle, gid: amend(handle, gid, "x")], ids=["retract", "edit"])
async def test_an_unknown_guidance_is_rejected(harness: StageHarness, act: Any) -> None:
    hold = harness.script.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    script_loop(harness, FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await act(handle, "nope")
    assert (await wait_for_state(harness, inp.task_id, "nope", "rejected"))["reason_code"] == "not_found"
    hold.release.set()
    await finish(handle)


async def test_sending_the_same_id_again_does_nothing(harness: StageHarness) -> None:
    """客户端重发（网络重试）不算新的一条。"""
    hold = harness.script.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    script_loop(harness, respond(read("a.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await steer(handle, "g1", "只看 a.py")
    await steer(handle, "g1", "只看 a.py")
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    hold.release.set()
    await finish(handle)
    assert told_the_model(loop_steps(harness, inp.task_id)[1]) == ["只看 a.py"]
    assert states(harness, inp.task_id, "g1") == ["pending", "applied"]


async def test_several_guidances_arrive_together_in_order(harness: StageHarness) -> None:
    hold = harness.script.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    script_loop(harness, respond(read("a.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await steer(handle, "g1", "第一条")
    await steer(handle, "g2", "第二条")
    await steer(handle, "g3", "第三条")
    await wait_for_state(harness, inp.task_id, "g3", "pending")
    hold.release.set()
    await finish(handle)
    assert told_the_model(loop_steps(harness, inp.task_id)[1]) == ["第一条", "第二条", "第三条"]
    assert [g["guidance_id"] for g in (await task_state(harness, inp.task_id))["guidance"]] == ["g1", "g2", "g3"]


async def test_at_most_twenty_are_pending(harness: StageHarness) -> None:
    hold = harness.script.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    script_loop(harness, FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    for i in range(21):
        await steer(handle, f"g{i}", f"第 {i} 条")
    assert (await wait_for_state(harness, inp.task_id, "g20", "rejected"))["reason_code"] == "too_many_pending"
    await wait_for_state(harness, inp.task_id, "g19", "pending")
    hold.release.set()
    await finish(handle)


# --- Planner 与 Reviewer 看得到；文字不外泄 ---


async def test_the_reviewers_view_carries_the_guidance() -> None:
    state = {"task_id": "t", "goal": "g", "done_when": "d", "guidance": [{"guidance_id": "g1", "text": "不要改接口", "mode": "next_step", "state": "applied", "applied_round": 1}]}
    view = reviewer_prompts.review_view(state, {"seq": 1, "goal": "g", "nodes": []}, {}, [])
    assert view["task_state"]["guidance"][0]["text"] == "不要改接口"
    assert "guidance" in reviewer_prompts._REVIEWER_RULES, "Reviewer 要知道据此验收"


async def test_the_planner_sees_the_guidance_but_not_the_highlights() -> None:
    from agentruntime_worker.planner import prompts as planner_prompts

    state = {
        "task_id": "t",
        "goal": "g",
        "done_when": "d",
        "guidance": [{"guidance_id": "g1", "text": "不要改接口", "mode": "next_step", "state": "applied", "applied_round": 1}],
        "rounds": [{"seq": 1, "stage_goal": "s", "highlights": {"answers": ["给人看的汇总"]}}],
    }
    text = planner_prompts._round("修 CI", 2, state, None, None, "")
    assert "不要改接口" in text
    assert "给人看的汇总" not in text


async def test_the_text_is_not_in_events_or_logs(harness: StageHarness, caplog: pytest.LogCaptureFixture) -> None:
    """纠偏的文字不进 span 与日志，事件也只带 id 与状态。"""
    secret = "纠偏原文只给模型看"
    caplog.set_level(logging.DEBUG)
    hold = harness.script.hold(f"{names.TOOL_LOOP_STEP}:fix.1")
    script_loop(harness, respond(read("a.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await steer(handle, "g1", secret)
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    await retract(handle, "g2")
    await amend(handle, "g3", secret)
    hold.release.set()
    await finish(handle)
    assert secret not in json.dumps(progress(harness, inp.task_id), ensure_ascii=False)
    assert secret not in caplog.text
