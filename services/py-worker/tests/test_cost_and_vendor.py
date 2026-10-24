"""Task 的成本口径与 judge 避开生成者（人定方案，2026-10-24）。

- R-01 评审第 4 节第 4 条、R-06 评审第 4 节第 4 条：Task 的成本与预算算上 tool_loop 每一步、Planner、Reviewer 与批量子图的花费
  （之前只算 Atomic Node 的执行）；tool_loop 每一步之前查 Task 的预算，用完就停（版本分支 r01-audit-and-budget）。
- R-02 评审第 4 节第 1 条：Node 的结果带上实际服务它的 vendor（Node Executor 的 meta、tool_loop 的每一步），Reviewer 调用模型时
  avoid_vendor 填生成者里最多的那一家（附录 H.3）。
"""

from __future__ import annotations

from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from agentruntime.common.v1 import enums_pb2

from agentruntime_worker import names
from agentruntime_worker.activities.types import NodeInput, PlanRoundOutput, ReviewRoundOutput, StagePolicy, StageResult, TemplateExpansion, ToolLoopStep
from agentruntime_worker.planner.activities import PlannerActivities
from agentruntime_worker.planner.context import StaticContextSource
from agentruntime_worker.planner.planner import Planner

from .planner_support import FakeGateway, context, plan_answer, signals
from .reviewer_support import FakeReviewGateway, answer
from .stage_harness import Script, StageHarness, harness  # noqa: F401 — harness 是 fixture
from .test_planner_activity import plan_input
from .test_reviewer_activity import activities as reviewer_activities
from .test_reviewer_activity import review_input
from .test_stage_runner import atomic, deliver, goal, one_round, results, scripted_steps

TASK_BUDGET = 2.0  # stage_harness 的 Task 预算（support.task_input）


def done_with_cost(cost: float, deliver_id: str = "answer") -> Any:
    async def review(inp: Any) -> ReviewRoundOutput:
        return ReviewRoundOutput(verdict={"verdict": "done", "reason": "脚本：完成", "done": {"deliver_node": deliver_id}}, cost_usd=cost)

    return review


def round_costs(s: Script) -> list[float]:
    [final] = s.finalized
    return [float(r.get("cost_usd") or 0.0) for r in final.task_state["rounds"]]


def with_meta(result: Any, **meta: Any) -> Any:
    async def behave(inp: NodeInput, _: Script) -> dict[str, Any]:
        return {"result": result, "meta": meta}

    return behave


# --- 成本口径（Temporal） ---


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_tool_loop_steps_count_toward_the_round_cost(harness: StageHarness) -> None:
    s = harness.script
    s.tool_steps["fix"] = scripted_steps(
        ToolLoopStep(action="tool", tool="repo_read", args={"path": "a.py"}, cost_usd=0.3),
        ToolLoopStep(action="finish", result={"patch": "diff"}, cost_usd=0.2),
    )
    one_round(harness, goal("fix", "tool_loop", tools=["repo_read"], max_iterations=5), deliver(after=["fix"]))
    _, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert round_costs(s) == [pytest.approx(0.5)], "tool_loop 每一步的模型花费都计入 Round 的成本"


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_planner_and_reviewer_costs_count_toward_the_round_cost(harness: StageHarness) -> None:
    s = harness.script
    s.plan_cost = 0.25
    s.reviewer = done_with_cost(0.05)
    s.behaviors["answer"] = with_meta({"text": "答案"}, cost_usd=0.1)
    one_round(harness, deliver())
    _, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert round_costs(s) == [pytest.approx(0.4)], "规划 0.25 + 执行 0.1 + 评审 0.05"
    before = [c for c in s.hook_calls if c.mount == "before_node" and c.subject.get("node", {}).get("id") == "answer"]
    assert before[0].subject["budget"]["spent_usd"] == pytest.approx(0.25), "这一轮的规划花费在执行前就算进已花的钱"


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_tool_loop_stops_when_the_task_budget_is_spent(harness: StageHarness) -> None:
    s = harness.script
    s.tool_steps["fix"] = scripted_steps(ToolLoopStep(action="tool", tool="repo_read", args={}, cost_usd=0.8))
    one_round(harness, goal("fix", "tool_loop", tools=["repo_read"], max_iterations=20), deliver(after=["fix"]))
    inp, result = await harness.run()
    steps = [c for c in s.calls if c.activity == names.TOOL_LOOP_STEP and c.workflow_id == inp.task_id]
    assert len(steps) == 3, "0.8、1.6、2.4：第三步之后预算用完，不再请求下一步"
    r = results(harness, inp.task_id)["fix"]
    assert (r.status, r.failure_class) == ("failed", "denied") and "预算" in r.error
    assert result.status == enums_pb2.TASK_STATUS_ABORTED


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_the_tool_loop_budget_counts_this_rounds_planning(harness: StageHarness) -> None:
    s = harness.script
    s.plan_cost = 1.5
    s.tool_steps["fix"] = scripted_steps(ToolLoopStep(action="tool", tool="repo_read", args={}, cost_usd=0.3))
    one_round(harness, goal("fix", "tool_loop", tools=["repo_read"], max_iterations=20), deliver(after=["fix"]))
    inp, _ = await harness.run()
    steps = [c for c in s.calls if c.activity == names.TOOL_LOOP_STEP and c.workflow_id == inp.task_id]
    assert len(steps) == 2, "1.5 + 0.3 + 0.3 = 2.1：第二步之后超过预算 2.0"


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_batched_subgraphs_bring_their_cost_back(harness: StageHarness) -> None:
    s = harness.script
    s.policy = StagePolicy(max_parallel=4, max_inline_nodes=2, batch_size=1, batch_parallel=2)
    step = atomic("label", template="label@v1", inputs={"ticket": "$item.id"})
    s.recipes["label_all@v1"] = TemplateExpansion(per_item=[step], items=[{"id": f"T-{i}"} for i in range(3)], collect="label")
    s.behaviors["label@v1"] = with_meta({"label": "x"}, cost_usd=0.1)
    one_round(harness, goal("batch", "template", recipe="label_all@v1"), deliver(after=["batch"]))
    _, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert round_costs(s) == [pytest.approx(0.3)], "三个条目各在一个子图里跑，花费都带回父 Stage"


# --- vendor（Temporal） ---


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_model_nodes_result_carries_the_vendor_that_served_it(harness: StageHarness) -> None:
    s = harness.script
    s.behaviors["answer"] = with_meta({"text": "答案"}, cost_usd=0.1, vendor="anthropic", tier="mid@low")
    one_round(harness, deliver())
    inp, _ = await harness.run()
    r = results(harness, inp.task_id)["answer"]
    assert (r.vendor, r.cost_usd) == ("anthropic", pytest.approx(0.1))


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_tool_loops_result_carries_the_vendor_of_its_steps(harness: StageHarness) -> None:
    s = harness.script
    s.tool_steps["fix"] = scripted_steps(
        ToolLoopStep(action="tool", tool="repo_read", args={}, vendor="openai"),
        ToolLoopStep(action="finish", result={"patch": "diff"}, vendor="openai"),
    )
    one_round(harness, goal("fix", "tool_loop", tools=["repo_read"], max_iterations=5), deliver(after=["fix"]))
    inp, _ = await harness.run()
    assert results(harness, inp.task_id)["fix"].vendor == "openai"


# --- activity（不需要 Temporal 服务） ---


async def test_plan_round_reports_what_planning_cost() -> None:
    gateway = FakeGateway(triage=signals(confidence=0.6), plan=[plan_answer()], cost_micros={"triage": 200, "plan": 20_000})
    out = await ActivityEnvironment().run(PlannerActivities(Planner(gateway), StaticContextSource(context())).plan_round, plan_input())
    assert isinstance(out, PlanRoundOutput)
    assert out.cost_usd == pytest.approx(0.0202), "分诊与规划两次调用"


async def test_review_round_reports_what_reviewing_cost() -> None:
    gateway = FakeReviewGateway([answer({"verdict": "continue"})], cost_micros=30_000)
    out = await ActivityEnvironment().run(reviewer_activities(gateway).review_round, review_input())
    assert out.cost_usd == pytest.approx(0.03)


async def test_the_reviewer_avoids_the_vendor_that_produced_the_results() -> None:
    gateway = FakeReviewGateway([answer({"verdict": "continue"})])
    produced = {
        "q_store": StageResult(role="fetch", status="succeeded", result={"rows": 30}, vendor="anthropic"),
        "q_basket": StageResult(role="fetch", status="succeeded", result={"rows": 30}, vendor="anthropic"),
        "note": StageResult(role="transform", status="succeeded", result={"text": "x"}, vendor="openai"),
    }
    await ActivityEnvironment().run(reviewer_activities(gateway).review_round, review_input(results=produced))
    [request] = gateway.requests
    assert request.call.avoid_vendor == "anthropic", "生成者里最多的那一家"


async def test_without_model_generated_results_the_reviewer_avoids_nobody() -> None:
    gateway = FakeReviewGateway([answer({"verdict": "continue"})])
    await ActivityEnvironment().run(reviewer_activities(gateway).review_round, review_input())
    assert gateway.requests[0].call.avoid_vendor == "", "program 节点的结果没有 vendor"


async def test_vendor_ties_are_broken_by_name() -> None:
    gateway = FakeReviewGateway([answer({"verdict": "continue"})])
    produced = {
        "a": StageResult(role="fetch", status="succeeded", result={}, vendor="openai"),
        "b": StageResult(role="fetch", status="succeeded", result={}, vendor="anthropic"),
    }
    await ActivityEnvironment().run(reviewer_activities(gateway).review_round, review_input(results=produced))
    assert gateway.requests[0].call.avoid_vendor == "anthropic", "并列时取名字靠前的，结果确定"
