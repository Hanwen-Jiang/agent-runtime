"""D16：tool_loop 里每次调用不可逆工具前都插入人工审批（设计书 3.2.3、附录 D.4、E.1、F.3；计划 S-05“审批双保险”）。

- 工具的 effect 为 write_irreversible 时，先经 HumanChannel 问人；问题里写清工具与具体参数。审批 id 是这次调用的
  Node id 加“.1”（第 i 步的调用是 <Goal id>.<2i>，与 Atomic Node 前的审批 <id>.1 同一规则），重放时不变。
- 通过才执行，执行时带上审批记录；只读与可回滚写的工具不问。
- 拒绝时不执行，把审批人的指导作为这一步的观察送回循环；超时按拒绝处理并撤回问题。
- 同一个 Goal Node 被拒 MAX_TOOL_REJECTIONS 次就记为 denied，交给 Reviewer。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentruntime.common.v1 import enums_pb2
from agentruntime.workflow.v1 import task_pb2

from agentruntime_worker import names
from agentruntime_worker.activities.types import StagePolicy, ToolLoopStep
from agentruntime_worker.workflows.stage_runner import MAX_TOOL_REJECTIONS

from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .test_stage_runner import deliver, goal, one_round, results, scripted_steps

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

SEARCH = ToolLoopStep(action="tool", tool="flight_search", args={"route": "PEK-SHA", "date": "2026-10-27"})
CHARGE = ToolLoopStep(action="tool", tool="card_charge", args={"flight": "MU5101", "amount_cny": 980})
CHARGE_CHEAPER = ToolLoopStep(action="tool", tool="card_charge", args={"flight": "HO1253", "amount_cny": 860})
EDIT = ToolLoopStep(action="tool", tool="repo_edit", args={"path": "a.py"})
FINISH = ToolLoopStep(action="finish", result={"ticket": "781-2345678901"})
IRREVERSIBLE = enums_pb2.EFFECT_WRITE_IRREVERSIBLE


def booking(h: StageHarness, *steps: ToolLoopStep, max_iterations: int = 8) -> None:
    s = h.script
    s.tools.update({"flight_search": "read", "card_charge": "write_irreversible", "repo_edit": "write_reversible"})
    s.tool_steps["book"] = scripted_steps(*steps)
    one_round(
        h,
        goal("book", "tool_loop", tools=["flight_search", "card_charge", "repo_edit"], effect_ceiling="write_irreversible", max_iterations=max_iterations),
        deliver(after=["book"]),
    )


def questions(h: StageHarness, task_id: str) -> list[Any]:
    return [q for q in h.script.questions if q.task_id == task_id]


async def wait_for_question(h: StageHarness, task_id: str, question_id: str) -> Any:
    await h.wait_for(lambda: any(q.question_id == question_id for q in questions(h, task_id)), f"审批问题 {question_id}")
    return next(q for q in questions(h, task_id) if q.question_id == question_id)


def tool_calls(h: StageHarness, task_id: str, tool: str | None = None) -> list[Any]:
    return [c for c in h.script.calls if c.activity == names.CALL_TOOL and c.workflow_id == task_id and (tool is None or c.inp.tool == tool)]


def loop_steps(h: StageHarness, task_id: str) -> list[Any]:
    return [c for c in h.script.calls if c.activity == names.TOOL_LOOP_STEP and c.workflow_id == task_id]


def reply(question_id: str, option: str, *, guidance: str = "", by: str = "u7") -> task_pb2.HumanReplySignal:
    return task_pb2.HumanReplySignal(question_id=question_id, option=option, guidance=guidance, replied_by_user_id=by)


async def test_an_irreversible_tool_call_waits_for_approval_and_carries_the_gate(harness: StageHarness) -> None:
    booking(harness, SEARCH, CHARGE, FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    # 第 2 步的工具调用是 book.4，审批是 book.4.1。
    q = await wait_for_question(harness, inp.task_id, "book.4.1")
    await asyncio.sleep(0.5)
    assert [c.inp.tool for c in tool_calls(harness, inp.task_id)] == ["flight_search"], "审批通过之前不调用不可逆工具"
    assert "card_charge" in q.text and "MU5101" in q.text and "980" in q.text, f"审批问题要写清工具与参数：{q.text}"
    assert tuple(q.options) == ("approve", "reject")
    await handle.signal(names.HUMAN_REPLY_SIGNAL, reply("book.4.1", "approve"))
    result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    [charge] = tool_calls(harness, inp.task_id, "card_charge")
    assert charge.node_id == "book.4"
    gate = charge.inp.gate
    assert gate is not None and (gate.gate_node, gate.option, gate.approved_by) == ("book.4.1", "approve", "u7")
    assert [q.question_id for q in questions(harness, inp.task_id)] == ["book.4.1"], "只读工具不问人"
    assert tool_calls(harness, inp.task_id, "flight_search")[0].inp.gate is None


async def test_read_and_reversible_tool_calls_do_not_ask(harness: StageHarness) -> None:
    booking(harness, SEARCH, EDIT, FINISH)
    inp, _ = await harness.run(effect_ceiling=IRREVERSIBLE)
    assert questions(harness, inp.task_id) == []
    assert [c.inp.tool for c in tool_calls(harness, inp.task_id)] == ["flight_search", "repo_edit"]
    assert results(harness, inp.task_id)["book"].status == "succeeded"


async def test_a_rejection_goes_back_to_the_loop_with_guidance(harness: StageHarness) -> None:
    booking(harness, CHARGE, CHARGE_CHEAPER, FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await wait_for_question(harness, inp.task_id, "book.2.1")
    await handle.signal(names.HUMAN_REPLY_SIGNAL, reply("book.2.1", "reject", guidance="超预算，换一班 900 元以内的"))
    await wait_for_question(harness, inp.task_id, "book.4.1")
    await handle.signal(names.HUMAN_REPLY_SIGNAL, reply("book.4.1", "approve"))
    result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    charges = tool_calls(harness, inp.task_id, "card_charge")
    assert [c.node_id for c in charges] == ["book.4"], "被拒的那次不执行"
    assert charges[0].inp.args == CHARGE_CHEAPER.args
    second_thought = harness.script.call("book.3").inp
    assert second_thought.history == [
        {"tool": "card_charge", "args": CHARGE.args, "rejected": True, "guidance": "超预算，换一班 900 元以内的", "replied_by": "u7"}
    ], "拒绝作为这一步的观察送回循环，带上审批人的指导"


async def test_repeated_rejections_deny_the_goal_node(harness: StageHarness) -> None:
    booking(harness, CHARGE, CHARGE_CHEAPER, FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    for i in range(1, MAX_TOOL_REJECTIONS + 1):
        qid = f"book.{2 * i}.1"
        await wait_for_question(harness, inp.task_id, qid)
        await handle.signal(names.HUMAN_REPLY_SIGNAL, reply(qid, "reject", guidance="不要买"))
    await asyncio.wait_for(handle.result(), timeout=30)
    r = results(harness, inp.task_id)
    assert r["book"].status == "failed" and r["book"].failure_class == "denied", r["book"]
    assert "拒绝" in r["book"].error
    assert r["answer"].status == "skipped"
    assert tool_calls(harness, inp.task_id, "card_charge") == []
    assert len(loop_steps(harness, inp.task_id)) == MAX_TOOL_REJECTIONS, "被拒到上限就停，不再让 agent 继续提议"


async def test_a_reply_sent_while_the_question_is_still_being_asked_is_not_lost(harness: StageHarness) -> None:
    """问题刚发出就到的回复（信号先于发问 activity 的结果到达工作流）也要收下，不能当作“不匹配”丢掉。"""
    client = harness.client

    async def approve_at_once(q: Any) -> None:
        await client.get_workflow_handle(q.task_id).signal(names.HUMAN_REPLY_SIGNAL, reply(q.question_id, "approve"))

    harness.script.on_question = approve_at_once
    harness.script.policy = StagePolicy(approval_timeout_s=10)
    booking(harness, CHARGE, FINISH)
    inp, result = await harness.run(effect_ceiling=IRREVERSIBLE)
    assert result.status == enums_pb2.TASK_STATUS_DONE, results(harness, inp.task_id)["book"]
    [charge] = tool_calls(harness, inp.task_id, "card_charge")
    assert charge.inp.gate is not None and charge.inp.gate.gate_node == "book.2.1"


async def test_an_approval_timeout_counts_as_a_rejection(harness: StageHarness) -> None:
    harness.script.policy = StagePolicy(approval_timeout_s=1)
    booking(harness, CHARGE, CHARGE_CHEAPER, FINISH)
    inp, _ = await harness.run(effect_ceiling=IRREVERSIBLE)
    r = results(harness, inp.task_id)
    assert r["book"].status == "failed" and r["book"].failure_class == "denied", r["book"]
    assert tool_calls(harness, inp.task_id, "card_charge") == []
    assert {"book.2.1", "book.4.1"} <= set(harness.script.withdrawn), "超时后撤回问题"
    second_thought = harness.script.call("book.3").inp
    assert second_thought.history == [{"tool": "card_charge", "args": CHARGE.args, "rejected": True, "timed_out": True}]
