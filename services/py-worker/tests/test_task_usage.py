"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 11.11 节问题 1：Task 的用量。

- 美元的用量按 task_state.schema.json 的口径，是各 Round 的 cost_usd 之和；Task 工作流把它填进 TaskWorkflowResult.usage
  （done、abort 都填；token 数这一期不填）。
- 正在跑的 Round 在 task_state 查询的结果里带上已经花的钱（规划加上已完成的节点），task-service 的 QueryTask 据此给出用量。
  只改查询给出的副本，不写进交给 activity 的 Task State（Stage Runner 另外把规划的花费算进已花的钱，写进去会重复计）。
- 不改命令序列，不用版本分支。
"""

from __future__ import annotations

import dataclasses

import pytest

from agentruntime.common.v1 import enums_pb2

from agentruntime_worker import names
from agentruntime_worker.activities.types import ToolLoopStep

from .stage_harness import StageHarness, harness, slow  # noqa: F401 — harness 是 fixture
from .test_cost_and_vendor import done_with_cost, round_costs, with_meta
from .test_stage_runner import deliver, goal, one_round, scripted_steps
from .test_task_budgets import use_repo_read
from .test_task_verdicts import out, scripted, task_state

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]


async def test_a_done_task_reports_the_sum_of_its_round_costs(harness: StageHarness) -> None:
    s = harness.script
    s.plan_cost = 0.25
    s.reviewer = done_with_cost(0.05)
    s.behaviors["answer"] = with_meta({"text": "答案"}, cost_usd=0.1)
    one_round(harness, deliver())
    _, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert result.HasField("usage"), "TaskWorkflowResult.usage 要填"
    assert result.usage.cost_usd_micros == 400_000, "规划 0.25 + 节点 0.1 + 评审 0.05"
    assert result.usage.cost_usd_micros == round(sum(round_costs(s)) * 1_000_000), "与 Task State 各 Round 的 cost_usd 之和一致"
    assert result.usage.input_tokens == 0 and result.usage.output_tokens == 0, "token 数这一期不填"


async def test_an_aborted_task_reports_its_usage_too(harness: StageHarness) -> None:
    s = harness.script
    s.plan_cost = 0.25
    s.behaviors["answer"] = with_meta({"text": "半成品"}, cost_usd=0.1)
    s.reviewer = scripted(dataclasses.replace(out("abort", "做不下去", abort={"completed": [], "not_completed": ["答案"]}), cost_usd=0.05))
    one_round(harness, deliver())
    _, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_ABORTED
    assert result.usage.cost_usd_micros == 400_000


async def test_the_running_round_shows_what_it_has_spent_so_far(harness: StageHarness) -> None:
    s = harness.script
    s.plan_cost = 0.25
    s.reviewer = done_with_cost(0.05)
    use_repo_read(harness)
    s.tool_steps["explore"] = scripted_steps(
        ToolLoopStep(action="tool", tool="repo_read", args={"path": "a.py"}, cost_usd=0.1),
        ToolLoopStep(action="finish", result={"found": "a.py"}, cost_usd=0.05),
    )
    s.behaviors["answer"] = slow(3, with_meta({"text": "答案"}, cost_usd=0.2))
    one_round(harness, goal("explore", "tool_loop", tools=["repo_read"], max_iterations=5), deliver(after=["explore"]))
    inp, handle = await harness.start()
    await harness.wait_for(lambda: any(c.node_id == "answer" for c in s.executed(inp.task_id)), "交付节点开始执行")

    state = await task_state(harness, inp.task_id)
    current = state["rounds"][-1]
    assert current["status"] == "running"
    assert current.get("cost_usd") == pytest.approx(0.4), "正在跑的 Round 带上已花的钱：规划 0.25 + tool_loop 两步 0.15"

    steps = [c.inp for c in s.calls if c.activity == names.TOOL_LOOP_STEP and c.workflow_id.startswith(inp.task_id)]
    assert steps, "tool_loop 跑过"
    assert "cost_usd" not in steps[-1].task_state["rounds"][-1], "交给 activity 的 Task State 不带正在跑的 Round 的花费（会被重复计）"

    result = await handle.result()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert result.usage.cost_usd_micros == 650_000, "0.25 + 0.15 + 0.2 + 0.05"
