"""docs/plan/running-task-interaction.md 第 1 节验收：一次模型回答可以调用多个工具。

现象：ModelToolLoop 只取 tool_calls[0]，系统提示也要求一步只调一个，于是每个工具调用都多一次模型请求（每次都带上全部上下文）。

- ToolLoopStep 带上全部调用（calls）；tool、args 仍是第一个，旧的工作流只用它。
- 连续的只读调用并发，每批的第一个先单独跑（它建好环境，其余的共用，否则默认配额下第三个会被拒）；结果按模型给的顺序写进 history。
- 写操作按顺序一个一个跑；写的失败或被拒，这次回答里后面的调用都不执行，记一条说明。读的失败不影响别的读。
- 不可逆工具逐个审批；一次最多 MAX_TOOL_CALLS_PER_STEP 个；工具调用预算用完就停。
- 调用 id：第一个 fix.2（不变），之后 fix.2.2、fix.2.3。只有一个调用的回答，记录与原来完全一样。
- Context Builder 把同一 step 的调用放进一条 assistant 消息；max_iterations 与重复错误都按模型的步数算。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from google.protobuf import struct_pb2
from temporalio.api.enums.v1 import EventType

from agentruntime.common.v1 import enums_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker import names
from agentruntime_worker.activities.types import ToolLoopStep
from agentruntime_worker.executor.tool_loop import SYSTEM_PROMPT
from agentruntime_worker.workflows import patches
from agentruntime_worker.workflows import stage_plan as sp

from .context_support import ContextGateway
from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .test_sandbox_sessions import S1, Setup
from .test_stage_runner import deliver, goal, one_round, results, scripted_steps
from .test_task_budgets import budget, budget_questions, events
from .test_task_verdicts import out, reply_with, scripted
from .test_tool_loop_approval import IRREVERSIBLE, loop_steps, questions, reply, tool_calls, wait_for_question
from .test_tool_loop_model import loop, step_input

FINISH = ToolLoopStep(action="finish", result={"done": True})


def read(path: str) -> dict[str, Any]:
    return {"tool": "repo_read", "args": {"path": path}}


def edit(path: str) -> dict[str, Any]:
    return {"tool": "repo_edit", "args": {"path": path}}


def respond(*calls: dict[str, Any]) -> ToolLoopStep:
    """模型一次回答里的多个工具调用。"""
    return ToolLoopStep(action="tool", tool=calls[0]["tool"], args=calls[0]["args"], calls=list(calls))


def script_loop(h: StageHarness, *steps: ToolLoopStep, tools: tuple[str, ...] = ("repo_read", "repo_edit"), ceiling: str = "write_reversible") -> None:
    s = h.script
    s.tools.update({"repo_read": "read", "repo_edit": "write_reversible"})
    s.tool_results.setdefault("repo_read", lambda i: {"content": i.args["path"]})
    s.tool_results.setdefault("repo_edit", lambda i: {"edited": i.args["path"]})
    s.tool_steps["fix"] = scripted_steps(*steps)
    one_round(h, goal("fix", "tool_loop", tools=list(tools), effect_ceiling=ceiling, max_iterations=6), deliver(after=["fix"]))


def paths(calls: list[Any]) -> list[str]:
    return [c.inp.args["path"] for c in calls]


def history_after_first_response(h: StageHarness, task_id: str) -> list[dict[str, Any]]:
    return loop_steps(h, task_id)[1].inp.history


async def call_tool_timeline(handle: Any) -> list[tuple[str, int]]:
    """事件历史里 call_tool 的安排与完成，按发生的顺序：("scheduled" 或 "completed", 第几个调用，从 0 开始)。"""
    history = await handle.fetch_history()
    scheduled: dict[int, int] = {}
    timeline: list[tuple[str, int]] = []
    for event in history.events:
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            if event.activity_task_scheduled_event_attributes.activity_type.name == names.CALL_TOOL:
                scheduled[event.event_id] = len(scheduled)
                timeline.append(("scheduled", scheduled[event.event_id]))
        elif event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            sid = event.activity_task_completed_event_attributes.scheduled_event_id
            if sid in scheduled:
                timeline.append(("completed", scheduled[sid]))
    return timeline


# --- 工作流：请求数、顺序与并发 ---


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_one_response_runs_all_its_calls_and_costs_one_model_request(harness: StageHarness) -> None:
    script_loop(harness, respond(read("a.py"), read("b.py"), read("c.py")), FINISH)
    inp, result = await harness.run(effect_ceiling=IRREVERSIBLE)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert len(loop_steps(harness, inp.task_id)) == 2, "三个读只多一次模型请求，不是三次"
    assert sorted(paths(tool_calls(harness, inp.task_id))) == ["a.py", "b.py", "c.py"]
    history = history_after_first_response(harness, inp.task_id)
    assert [e["args"]["path"] for e in history] == ["a.py", "b.py", "c.py"], "结果按模型给的顺序"
    assert [e["observation"]["content"] for e in history] == ["a.py", "b.py", "c.py"]
    assert {e["step"] for e in history} == {1}, "同一次回答的调用带 step，Context Builder 据此放进一条消息"


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_each_call_has_its_own_id(harness: StageHarness) -> None:
    script_loop(harness, respond(read("a.py"), read("b.py"), read("c.py")), FINISH)
    inp, _ = await harness.run(effect_ceiling=IRREVERSIBLE)
    assert {c.node_id for c in tool_calls(harness, inp.task_id)} == {"fix.2", "fix.2.2", "fix.2.3"}, "幂等键含 Node id，所以要各不相同；第一个的 id 不变"


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_the_first_read_runs_alone_then_the_rest_run_together(harness: StageHarness) -> None:
    script_loop(harness, respond(read("a.py"), read("b.py"), read("c.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await asyncio.wait_for(handle.result(), timeout=60)
    timeline = await call_tool_timeline(harness.client.get_workflow_handle(inp.task_id))
    assert timeline[:2] == [("scheduled", 0), ("completed", 0)], "第一个先跑完：它建好环境，其余的共用"
    assert timeline[2:4] == [("scheduled", 1), ("scheduled", 2)], f"其余的一起发出，不等前一个完成：{timeline}"


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_writes_run_in_order_and_a_read_after_a_write_waits_for_it(harness: StageHarness) -> None:
    script_loop(harness, respond(read("a.py"), read("b.py"), edit("c.py"), read("d.py")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await asyncio.wait_for(handle.result(), timeout=60)
    assert paths(tool_calls(harness, inp.task_id)) == ["a.py", "b.py", "c.py", "d.py"]
    timeline = await call_tool_timeline(harness.client.get_workflow_handle(inp.task_id))
    assert timeline.index(("completed", 0)) < timeline.index(("scheduled", 2)) and timeline.index(("completed", 1)) < timeline.index(("scheduled", 2)), "写之前的读都做完"
    assert timeline.index(("completed", 2)) < timeline.index(("scheduled", 3)), "写之后的读等写完"


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_failed_write_stops_the_rest_of_that_response(harness: StageHarness) -> None:
    """后面的调用是按前面成功来写的。每个调用仍要有结果：模型的下一步要配对。"""
    script_loop(harness, respond(edit("c.py"), edit("d.py"), read("e.py")), FINISH)
    harness.script.tool_results["repo_edit"] = lambda i: RuntimeError("冲突") if i.args["path"] == "c.py" else {"edited": i.args["path"]}
    inp, result = await harness.run(effect_ceiling=IRREVERSIBLE)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert paths(tool_calls(harness, inp.task_id)) == ["c.py"], "后面的都不执行"
    history = history_after_first_response(harness, inp.task_id)
    assert [e["args"]["path"] for e in history] == ["c.py", "d.py", "e.py"]
    assert "冲突" in history[0]["error"] and not history[0].get("skipped")
    assert history[1]["skipped"] is True and history[2]["skipped"] is True and "没有执行" in history[1]["error"]


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_failed_read_does_not_stop_the_other_reads(harness: StageHarness) -> None:
    script_loop(harness, respond(read("gone.py"), read("a.py"), read("b.py")), FINISH)
    harness.script.tool_results["repo_read"] = lambda i: RuntimeError("file_not_found") if i.args["path"] == "gone.py" else {"content": i.args["path"]}
    inp, _ = await harness.run(effect_ceiling=IRREVERSIBLE)
    history = history_after_first_response(harness, inp.task_id)
    assert "file_not_found" in history[0]["error"]
    assert [e["observation"]["content"] for e in history[1:]] == ["a.py", "b.py"]


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_an_unknown_tool_only_fails_itself(harness: StageHarness) -> None:
    script_loop(harness, respond(read("a.py"), {"tool": "teleport", "args": {}}, read("b.py")), FINISH, tools=("repo_read",))
    inp, _ = await harness.run(effect_ceiling=IRREVERSIBLE)
    assert sorted(paths(tool_calls(harness, inp.task_id))) == ["a.py", "b.py"]
    history = history_after_first_response(harness, inp.task_id)
    assert history[1]["tool"] == "teleport" and "不在" in history[1]["error"]


# --- 审批、预算与上限 ---


def charges(h: StageHarness, *steps: ToolLoopStep) -> None:
    s = h.script
    s.tools.update({"card_charge": "write_irreversible"})
    s.tool_steps["book"] = scripted_steps(*steps)
    one_round(h, goal("book", "tool_loop", tools=["card_charge"], effect_ceiling="write_irreversible", max_iterations=6), deliver(after=["book"]))


def charge(flight: str) -> dict[str, Any]:
    return {"tool": "card_charge", "args": {"flight": flight}}


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_irreversible_calls_are_approved_one_at_a_time(harness: StageHarness) -> None:
    charges(harness, respond(charge("A1"), charge("B2")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await wait_for_question(harness, inp.task_id, "book.2.1")
    await asyncio.sleep(0.3)
    assert [q.question_id for q in questions(harness, inp.task_id)] == ["book.2.1"], "第一个有结果之前不问第二个"
    await handle.signal(names.HUMAN_REPLY_SIGNAL, reply("book.2.1", "approve"))
    await wait_for_question(harness, inp.task_id, "book.2.2.1")
    await handle.signal(names.HUMAN_REPLY_SIGNAL, reply("book.2.2.1", "approve"))
    result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert [(c.node_id, c.inp.gate.gate_node) for c in tool_calls(harness, inp.task_id)] == [("book.2", "book.2.1"), ("book.2.2", "book.2.2.1")]


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_rejected_call_stops_the_rest_of_that_response(harness: StageHarness) -> None:
    charges(harness, respond(charge("A1"), charge("B2")), FINISH)
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await wait_for_question(harness, inp.task_id, "book.2.1")
    await handle.signal(names.HUMAN_REPLY_SIGNAL, reply("book.2.1", "reject", guidance="别买"))
    result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert tool_calls(harness, inp.task_id) == [], "拒绝之后第二个也不执行"
    assert [q.question_id for q in questions(harness, inp.task_id)] == ["book.2.1"], "也不再问第二个"
    history = history_after_first_response(harness, inp.task_id)
    assert history[0]["rejected"] is True and history[1]["skipped"] is True


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_the_tool_call_budget_stops_within_a_response(harness: StageHarness) -> None:
    s = harness.script
    script_loop(harness, respond(read("a.py"), read("b.py"), read("c.py"), read("d.py")), FINISH)
    s.plans["main"].append({"seq": 2, "goal": "第 2 轮", "exit_when": "完成", "nodes": [deliver("answer2")]})
    s.reviewer = scripted(out("replan", "探索没完成，换个方向"))
    s.on_question = reply_with(harness, "stop_and_wrap_up")
    inp, _ = await harness.run(budget=budget(tool_calls=2), effect_ceiling=IRREVERSIBLE)
    assert len(tool_calls(harness, inp.task_id)) == 2, "预算是 2，第三个不发"
    fix = results(harness, inp.task_id)["fix"]
    assert fix.status == "failed" and fix.failure_class == "denied"
    finished = next(e for e in events(harness, inp.task_id, "node_finished") if e["node_id"] == "fix")
    assert finished["error_code"] == "budget_exhausted" and finished["params"] == {"budget": "tool_calls", "limit": 2, "used": 2}
    assert budget_questions(harness, inp.task_id), "下一轮照旧问人"


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_calls_over_the_limit_are_reported_not_run(harness: StageHarness) -> None:
    over = sp.MAX_TOOL_CALLS_PER_STEP + 2
    script_loop(harness, respond(*[read(f"f{i}.py") for i in range(over)]), FINISH)
    inp, _ = await harness.run(effect_ceiling=IRREVERSIBLE)
    assert len(tool_calls(harness, inp.task_id)) == sp.MAX_TOOL_CALLS_PER_STEP
    history = history_after_first_response(harness, inp.task_id)
    assert len(history) == over, "每个调用都要有结果，模型的下一步要配对"
    assert all(e["skipped"] is True and "一次最多" in e["error"] for e in history[sp.MAX_TOOL_CALLS_PER_STEP :])


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_each_call_reports_its_own_progress(harness: StageHarness) -> None:
    script_loop(harness, respond(read("a.py"), read("b.py"), read("c.py")), FINISH)
    inp, _ = await harness.run(effect_ceiling=IRREVERSIBLE)
    statuses = events(harness, inp.task_id, "tool_status")
    assert sorted(e["target"] for e in statuses if e["state"] == "running") == ["a.py", "b.py", "c.py"]
    assert sorted(e["target"] for e in statuses if e["state"] == "succeeded") == ["a.py", "b.py", "c.py"]
    assert {e["step"] for e in statuses} == {1}, "step 是模型的第几步，不是第几个调用"


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_single_call_is_recorded_exactly_as_before(harness: StageHarness) -> None:
    script_loop(harness, ToolLoopStep(action="tool", tool="repo_read", args={"path": "a.py"}), FINISH)
    inp, _ = await harness.run(effect_ceiling=IRREVERSIBLE)
    assert [c.node_id for c in tool_calls(harness, inp.task_id)] == ["fix.2"]
    assert history_after_first_response(harness, inp.task_id) == [{"tool": "repo_read", "args": {"path": "a.py"}, "observation": {"content": "a.py"}}]


def test_new_runs_take_the_parallel_path_behind_a_version_branch() -> None:
    assert patches.PARALLEL_TOOL_CALLS_PATCH in patches.AT_START


# --- 重复错误按模型的步数算 ---


def failed(step: int | None, error: str = "file_not_found", **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"tool": "repo_read", "args": {}, "error": error, **extra}
    if step is not None:
        entry["step"] = step
    return entry


def test_the_same_error_within_one_response_is_not_a_repeat() -> None:
    assert not sp.repeated_error([failed(1), failed(1), failed(1)]), "同一次回答里读三个不存在的文件，不是原地重试"


def test_the_same_error_across_steps_is_a_repeat() -> None:
    assert sp.repeated_error([failed(1), failed(1), failed(2), failed(3)])
    assert sp.repeated_error([failed(None), failed(None), failed(None)]), "没有 step 的旧记录照旧"


def test_skipped_calls_do_not_count_as_errors() -> None:
    assert not sp.repeated_error([failed(1, "没有执行", skipped=True), failed(2, "没有执行", skipped=True), failed(3, "没有执行", skipped=True)])


# --- 模型一侧 ---


def many(*calls: tuple[str, dict[str, Any]]) -> mg.GenerateResponse:
    tool_calls_: list[mg.ToolCall] = []
    for i, (name, args) in enumerate(calls):
        arguments = struct_pb2.Struct()
        arguments.update(args)
        tool_calls_.append(mg.ToolCall(id=f"call_{i}", name=name, arguments=arguments))
    return mg.GenerateResponse(finish_reason=mg.FINISH_REASON_TOOL_CALLS, tool_calls=tool_calls_, model=mg.ModelChoice(tier="strong@medium", vendor="mock"))


async def test_the_step_returns_every_call_the_model_made(tmp_path: Path) -> None:
    gateway = ContextGateway(steps=[many(("repo_read", {"path": "a.py"}), ("repo_grep", {"pattern": "x"}), ("repo_read", {"path": "b.py"}))])
    step = await loop(tmp_path, gateway).step(step_input([]))
    assert step.action == "tool"
    assert step.calls == [{"tool": "repo_read", "args": {"path": "a.py"}}, {"tool": "repo_grep", "args": {"pattern": "x"}}, {"tool": "repo_read", "args": {"path": "b.py"}}]
    assert (step.tool, step.args) == ("repo_read", {"path": "a.py"}), "旧的工作流只用第一个"


def test_the_prompt_asks_for_batched_reads() -> None:
    assert "一步只调用一个工具" not in SYSTEM_PROMPT
    assert "同一步" in SYSTEM_PROMPT, "互不依赖的读取要放在同一步一起调，否则模型还是一个一个来"


# --- Context Builder ---


async def test_calls_of_one_response_become_one_assistant_message(tmp_path: Path) -> None:
    history = [
        {"tool": "repo_read", "args": {"path": "a.py"}, "observation": {"content": "A"}, "step": 1},
        {"tool": "repo_read", "args": {"path": "b.py"}, "observation": {"content": "B"}, "step": 1},
        {"tool": "repo_read", "args": {"path": "c.py"}, "observation": {"content": "C"}},
    ]
    gateway = ContextGateway(steps=[{"done": True}])
    await loop(tmp_path, gateway).step(step_input(history))
    messages = list(gateway.requests[-1].generation.messages)
    actions = [i for i, m in enumerate(messages) if m.role == mg.CHAT_ROLE_ASSISTANT and m.tool_calls]
    assert [len(messages[i].tool_calls) for i in actions] == [2, 1], "同一 step 的放一条；没有 step 的旧记录各一条"
    first = actions[0]
    ids = [c.id for c in messages[first].tool_calls]
    assert [m.tool_call_id for m in messages[first + 1 : first + 3]] == ids, "tool 消息紧跟在后面，并且配得上"
    assert all(m.role == mg.CHAT_ROLE_TOOL for m in messages[first + 1 : first + 3])


# --- 第一个先跑的依据 ---


async def test_after_the_first_call_concurrent_reads_share_one_environment(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    tenant = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1", session_id=S1)
    spec = SandboxSpec(runtime="python", network="none", idle_timeout=timedelta(minutes=5), workspace_id="w1")
    busy = timedelta(minutes=2)
    first = await s.broker.acquire(tenant, spec, task_id="tsk", writable=False, reuse=True, busy_for=busy)
    await s.broker.release(first, keep=True)
    leases = await asyncio.gather(*[s.broker.acquire(tenant, spec, task_id="tsk", writable=False, reuse=True, busy_for=busy) for _ in range(4)])
    assert {lease.record.env_ref for lease in leases} == {first.record.env_ref}, "环境建好之后并发的读都复用它，不再各建一个"
    for lease in leases:
        await s.broker.release(lease, keep=True)
    assert len(s.envs("t1")) == 1


# --- 被拒的调用要让模型知道 ---


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"rejected": True, "guidance": "别买这一班"}, ("审批人拒绝", "别买这一班")),
        ({"rejected": True, "timed_out": True}, ("审批超时",)),
    ],
    ids=["guidance", "timeout"],
)
async def test_a_rejection_reaches_the_model(tmp_path: Path, entry: dict[str, Any], expected: tuple[str, ...]) -> None:
    """附录 E.1：拒绝时把审批人的指导作为这一步的观察送回循环。之前模型看到的只是“输出：null”。"""
    gateway = ContextGateway(steps=[{"done": True}])
    await loop(tmp_path, gateway).step(step_input([{"tool": "card_charge", "args": {"flight": "A1"}, **entry}]))
    [result] = [m for m in gateway.requests[-1].generation.messages if m.role == mg.CHAT_ROLE_TOOL]
    assert all(text in result.text for text in expected), result.text
    assert "null" not in result.text
