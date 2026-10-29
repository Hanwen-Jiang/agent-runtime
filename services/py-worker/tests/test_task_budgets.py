"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 3 节验收：Task 的时间与工具调用预算。

2026-10-28 人定：用完时先问人，不直接 abort。细化见该计划第 11 节：
- 关口：每个 Round 开头（规划或重跑之前）查时间与工具调用预算；tool_loop 每一步之前查时间、每次工具调用之前查调用次数，
  用完就停这个节点（failed、denied，error_code budget_exhausted），交给 Reviewer；评审照常（可能已经能交付）。
- 问人：question_id _budget，kind escalation，回复限定三种；question_code budget_exhausted，params 写明哪项预算、上限与用量。
  continue_as_is：用完的那项加一份（与原预算相同），接着规划；replan_with_guidance：同样加一份，Planner 拿到人的指导；
  stop_and_wrap_up 与超时没回复：abort，用现有结果收尾，verdict 事件带 reason_code budget_stopped、budget_no_reply。
- 时间只算 Agent 在跑的时间：等人回复（发问到收到回复）的时间不算。
- 工具调用次数：tool_loop 里每次工具调用，含 sub_agent 子工作流里的；写进 Round 的 tool_calls 与 Task State 的 budget_used。
- 预算为 0 或没给：不限。
- 新的运行走版本分支（patches.AT_START 末尾追加），旧历史照旧回放；golden 补“问人”与“工具调用用完停下”两份（test_stage_replay）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from google.protobuf import duration_pb2

from agentruntime.common.v1 import enums_pb2
from agentruntime.workflow.v1 import task_pb2

from agentruntime_worker import names
from agentruntime_worker.activities.types import HumanQuestion, StagePolicy, ToolLoopStep

from .reviewer_support import HUMAN_REPLIES
from .stage_harness import StageHarness, harness, slow  # noqa: F401 — harness 是 fixture
from .support import assert_valid
from .test_stage_runner import atomic, deliver, goal, one_round, results, scripted_steps, stage
from .test_task_verdicts import out, reply_with, scripted, task_state

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

USD = 2_000_000


def budget(*, tool_calls: int = 0, wall_clock_s: float | None = None) -> task_pb2.Budget:
    b = task_pb2.Budget(usd_micros=USD, tool_calls=tool_calls)
    if wall_clock_s is not None:
        b.wall_clock.CopyFrom(duration_pb2.Duration(seconds=int(wall_clock_s), nanos=int((wall_clock_s % 1) * 1e9)))
    return b


def reading(n: int, *, then_finish: bool = True) -> Callable[[Any], ToolLoopStep]:
    """前 n 步各读一个文件，之后完成（then_finish 为 False 时一直读）。"""

    def step(inp: Any) -> ToolLoopStep:
        if inp.iteration <= n or not then_finish:
            return ToolLoopStep(action="tool", tool="repo_read", args={"path": f"f{inp.iteration}.py"})
        return ToolLoopStep(action="finish", result={"read": n})

    return step


def tool_calls_of(h: StageHarness, task_id: str) -> list[Any]:
    return [c for c in h.script.calls if c.activity == names.CALL_TOOL and c.workflow_id.startswith(task_id)]


def events(h: StageHarness, task_id: str, kind: str) -> list[dict[str, Any]]:
    return [e for e in h.script.progress(task_id) if e["event"] == kind]


def budget_questions(h: StageHarness, task_id: str) -> list[HumanQuestion]:
    return [q for q in h.script.questions if q.task_id == task_id and q.question_id == "_budget"]


def after(seconds: float, answer: Callable[[HumanQuestion], Awaitable[None]]) -> Callable[[HumanQuestion], Awaitable[None]]:
    async def later(q: HumanQuestion) -> None:
        await asyncio.sleep(seconds)
        await answer(q)

    return later


def use_repo_read(h: StageHarness) -> None:
    h.script.tools["repo_read"] = "read"
    h.script.tool_results["repo_read"] = lambda i: {"content": "x = 1"}


# --- 写进 Task State ---


async def test_budgets_and_usage_are_in_the_task_state(harness: StageHarness) -> None:
    s = harness.script
    use_repo_read(harness)
    s.tool_steps["explore"] = reading(2)
    one_round(harness, goal("explore", "tool_loop", tools=["repo_read"], max_iterations=10), deliver(after=["explore"]))
    inp, result = await harness.run(budget=budget(tool_calls=7, wall_clock_s=1200))
    assert result.status == enums_pb2.TASK_STATUS_DONE
    state = await task_state(harness, inp.task_id)
    assert_valid("task_state", state)
    assert state["constraints"]["tool_calls"] == 7 and state["constraints"]["wall_clock_min"] == 20
    used = state["budget_used"]
    assert used["tool_calls"] == 2 and used.get("extensions", 0) == 0
    assert 0 <= used["wall_clock_min"] < 1
    assert state["rounds"][0]["tool_calls"] == 2
    assert s.plan_inputs[0].task_state["constraints"]["tool_calls"] == 7, "Planner 看得到时间与工具调用的预算"


async def test_no_budget_means_no_limit(harness: StageHarness) -> None:
    s = harness.script
    use_repo_read(harness)
    s.tool_steps["explore"] = reading(25)
    one_round(harness, goal("explore", "tool_loop", tools=["repo_read"], max_iterations=30), deliver(after=["explore"]))
    inp, result = await harness.run(budget=budget())
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert len(tool_calls_of(harness, inp.task_id)) == 25 and not budget_questions(harness, inp.task_id)
    state = await task_state(harness, inp.task_id)
    assert "tool_calls" not in state["constraints"] and "wall_clock_min" not in state["constraints"]
    assert state["budget_used"]["tool_calls"] == 25


# --- 工具调用次数 ---


async def test_tool_loop_stops_before_the_call_that_would_exceed_the_budget_and_the_next_round_asks(harness: StageHarness) -> None:
    s = harness.script
    use_repo_read(harness)
    s.tool_steps["explore"] = reading(10, then_finish=False)
    s.plans["main"] = [stage(1, goal("explore", "tool_loop", tools=["repo_read"], max_iterations=10), deliver(after=["explore"])), stage(2, deliver("answer2"))]
    s.reviewer = scripted(out("replan", "探索没完成，换个方向"))
    s.on_question = reply_with(harness, "stop_and_wrap_up")
    inp, result = await harness.run(budget=budget(tool_calls=3))

    assert len(tool_calls_of(harness, inp.task_id)) == 3, "第 4 次调用之前就停"
    explore = results(harness, inp.task_id)["explore"]
    assert explore.status == "failed" and explore.failure_class == "denied"
    finished = next(e for e in events(harness, inp.task_id, "node_finished") if e["node_id"] == "explore")
    assert finished["error_code"] == "budget_exhausted"
    assert finished["params"] == {"budget": "tool_calls", "limit": 3, "used": 3}

    asked = budget_questions(harness, inp.task_id)
    assert len(asked) == 1 and asked[0].round_seq == 2 and list(asked[0].options) == list(HUMAN_REPLIES)
    required = [e for e in events(harness, inp.task_id, "human_input_required") if e["question_id"] == "_budget"]
    assert len(required) == 1
    assert required[0]["kind"] == "escalation" and required[0]["question_code"] == "budget_exhausted"
    assert required[0]["params"] == {"budget": "tool_calls", "limit": 3, "used": 3}

    assert len(s.plan_inputs) == 1, "人选择停止：不再规划"
    assert result.status == enums_pb2.TASK_STATUS_ABORTED and result.final_verdict == enums_pb2.VERDICT_ABORT
    last = events(harness, inp.task_id, "verdict")[-1]
    assert last["verdict"] == "abort" and last["reason_code"] == "budget_stopped" and last["params"]["budget"] == "tool_calls"
    for e in harness.script.progress(inp.task_id):
        assert_valid("task_event_payload", e)


async def test_continuing_adds_one_more_allotment(harness: StageHarness) -> None:
    s = harness.script
    use_repo_read(harness)
    s.tool_steps["explore"] = reading(3)
    s.tool_steps["fix"] = reading(2)
    s.plans["main"] = [
        stage(1, goal("explore", "tool_loop", tools=["repo_read"], max_iterations=10)),
        stage(2, goal("fix", "tool_loop", tools=["repo_read"], max_iterations=10), deliver(after=["fix"])),
    ]
    s.reviewer = scripted(out("continue", "探索完了"), out("done", "修好了", done={"deliver_node": "answer"}))
    s.on_question = reply_with(harness, "continue_as_is")
    inp, result = await harness.run(budget=budget(tool_calls=3))
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert [q.round_seq for q in budget_questions(harness, inp.task_id)] == [2], "第 1 轮正好用满：第 2 轮开头问一次"
    state = await task_state(harness, inp.task_id)
    assert state["constraints"]["tool_calls"] == 6, "加一份：与原预算相同"
    assert state["budget_used"] == {**state["budget_used"], "tool_calls": 5, "extensions": 1}
    assert [r.get("tool_calls") for r in state["rounds"]] == [3, 2]
    assert s.plan_inputs[1].task_state["constraints"]["tool_calls"] == 6, "Planner 看得到加过的预算"


async def test_replan_with_guidance_extends_and_passes_the_guidance_to_the_planner(harness: StageHarness) -> None:
    s = harness.script
    use_repo_read(harness)
    s.tool_steps["explore"] = reading(3)
    s.plans["main"] = [stage(1, goal("explore", "tool_loop", tools=["repo_read"], max_iterations=10)), stage(2, deliver("answer"))]
    s.reviewer = scripted(out("continue", "探索完了"), out("done", "完成", done={"deliver_node": "answer"}))
    s.on_question = reply_with(harness, "replan_with_guidance", guidance="只看 src 目录")
    inp, result = await harness.run(budget=budget(tool_calls=3))
    assert result.status == enums_pb2.TASK_STATUS_DONE
    second = s.plan_inputs[1]
    assert second.last_round["human_reply"] == {"option": "replan_with_guidance", "guidance": "只看 src 目录"}
    assert second.task_state["constraints"]["tool_calls"] == 6


async def test_an_unanswered_budget_question_stops_and_wraps_up(harness: StageHarness) -> None:
    s = harness.script
    use_repo_read(harness)
    s.policy = StagePolicy(human_timeout_s=1)
    s.tool_steps["explore"] = reading(3)
    s.plans["main"] = [stage(1, goal("explore", "tool_loop", tools=["repo_read"], max_iterations=10)), stage(2, deliver("answer"))]
    s.reviewer = scripted(out("continue", "探索完了"))
    inp, result = await harness.run(budget=budget(tool_calls=3))
    assert result.status == enums_pb2.TASK_STATUS_ABORTED and len(s.plan_inputs) == 1
    assert s.withdrawn == ["_budget"], "超时后撤回问题"
    last = events(harness, inp.task_id, "verdict")[-1]
    assert last["verdict"] == "abort" and last["reason_code"] == "budget_no_reply"


async def test_tool_calls_in_a_sub_agent_count(harness: StageHarness) -> None:
    s = harness.script
    use_repo_read(harness)
    s.tool_steps["research"] = scripted_steps(
        ToolLoopStep(action="tool", tool="repo_read", args={"path": "a.py"}),
        ToolLoopStep(action="tool", tool="repo_read", args={"path": "b.py"}),
        ToolLoopStep(action="finish", result={"summary": "两个文件"}),
    )
    one_round(harness, goal("research", "sub_agent", tools=["repo_read"]), deliver(after=["research"]))
    inp, result = await harness.run(budget=budget(tool_calls=50))
    assert result.status == enums_pb2.TASK_STATUS_DONE
    state = await task_state(harness, inp.task_id)
    assert state["rounds"][0]["tool_calls"] == 2 and state["budget_used"]["tool_calls"] == 2


# --- 时间 ---


async def test_time_spent_waiting_for_a_human_does_not_count(harness: StageHarness) -> None:
    s = harness.script
    use_repo_read(harness)
    s.behaviors["warmup"] = slow(3.5)
    s.plans["main"] = [stage(1, atomic("warmup", role="fetch", effect="read")), stage(2, deliver("answer"))]
    s.reviewer = scripted(out("continue", "取到了"), out("done", "完成", done={"deliver_node": "answer"}))
    # 人 6 秒后才回复：等人的时间算进去的话，加一份（共 6 秒）之后用量就超过上限了。
    s.on_question = after(6, reply_with(harness, "continue_as_is"))
    inp, result = await harness.run(budget=budget(wall_clock_s=3))
    assert result.status == enums_pb2.TASK_STATUS_DONE
    asked = budget_questions(harness, inp.task_id)
    assert len(asked) == 1 and asked[0].round_seq == 2
    required = next(e for e in events(harness, inp.task_id, "human_input_required") if e["question_id"] == "_budget")
    assert required["params"]["budget"] == "wall_clock"
    state = await task_state(harness, inp.task_id)
    assert state["budget_used"]["extensions"] == 1
    assert state["budget_used"]["wall_clock_min"] < state["constraints"]["wall_clock_min"], "等人的 6 秒不算"


async def test_tool_loop_does_not_start_another_step_once_time_is_up(harness: StageHarness) -> None:
    s = harness.script
    use_repo_read(harness)
    s.behaviors["warmup"] = slow(2.5)
    s.tool_steps["explore"] = reading(1)
    s.plans["main"] = [
        stage(1, atomic("warmup", role="fetch", effect="read"), goal("explore", "tool_loop", tools=["repo_read"], max_iterations=5, after=["warmup"])),
        stage(2, deliver("answer")),
    ]
    s.reviewer = scripted(out("continue", "超时了"))
    s.on_question = reply_with(harness, "stop_and_wrap_up")
    inp, result = await harness.run(budget=budget(wall_clock_s=2))
    steps = [c for c in s.calls if c.activity == names.TOOL_LOOP_STEP and c.workflow_id.startswith(inp.task_id)]
    assert not steps, "时间用完：tool_loop 不再请求下一步"
    finished = next(e for e in events(harness, inp.task_id, "node_finished") if e["node_id"] == "explore")
    assert finished["status"] == "failed" and finished["error_code"] == "budget_exhausted" and finished["params"]["budget"] == "wall_clock"
    assert result.status == enums_pb2.TASK_STATUS_ABORTED

