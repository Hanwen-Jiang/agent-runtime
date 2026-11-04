"""N16-R 第一段（docs/plan/sub-agent.md 第 6 节）：子 Agent 的份额在运行时生效，子 Agent 用 mid 档。

1. sub_agent 的 Node 写了 budget.usd（Planner 按份额写的）：子工作流的美元预算是 启动时已花的 + 它（不超过 Task 的）；
   用完了这个子 Agent 不再调工具、收尾交回部分结果（第三段），别的照常。没写时与原来一样，只受 Task 的预算约束。
2. 子 Agent（tool_loop 的 isolated 步）的模型调用用 mid 档（附录 F）；主流程的 tool_loop 不换（提示缓存）。开关关着时都用原来的档。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentruntime.common.v1 import enums_pb2
from agentruntime.workflow.v1 import task_pb2
from agentruntime_worker import names
from agentruntime_worker.activities.types import ToolLoopStep
from agentruntime_worker.context.builder import ContextBuilder
from agentruntime_worker.executor.tool_loop import SUB_AGENT_TIER, TOOL_LOOP_TIER, ModelToolLoop

from .context_support import ContextGateway, config, objects
from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .test_context_usage import answered
from .test_stage_runner import deliver, goal, one_round, scripted_steps
from .test_tool_loop_model import step_input


def spending_agent(h: StageHarness, nid: str, cost: float = 0.02) -> None:
    h.script.tools["repo_grep"] = "read"
    h.script.tool_results["repo_grep"] = lambda i: {"matches": []}
    h.script.tool_steps[nid] = scripted_steps(
        ToolLoopStep(action="tool", tool="repo_grep", args={"pattern": nid, "path": "src"}, cost_usd=cost),
        ToolLoopStep(action="tool", tool="repo_grep", args={"pattern": nid, "path": "lib"}, cost_usd=cost),
        ToolLoopStep(action="finish", result={"found": nid}, cost_usd=cost),
    )


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_sub_agent_stops_at_its_share(harness: StageHarness) -> None:  # noqa: F811
    spending_agent(harness, "small")
    spending_agent(harness, "free")
    one_round(
        harness,
        goal("small", "sub_agent", tools=["repo_grep"], budget={"usd": 0.03}),
        goal("free", "sub_agent", tools=["repo_grep"]),
        deliver(after=["small", "free"]),
    )
    inp, _ = await harness.run(budget=task_pb2.Budget(usd_micros=5_000_000))
    results = harness.script.review(inp.task_id, 1).stage_results
    # 第二步之后花了 0.04 > 0.03：不再调工具，收尾交回部分结果（第三段，SUB_AGENT_WRAP_UP_PATCH；见 test_sub_agent_wrap_up.py）。
    assert results["small"].status == "succeeded" and results["small"].result.get("partial") is True
    small_tools = [c for c in harness.script.calls if c.activity == names.CALL_TOOL and c.workflow_id.endswith("small")]
    assert len(small_tools) == 2, "份额用完后不再调工具"
    assert results["free"].status == "succeeded" and "partial" not in results["free"].result, "没写份额的照常跑完"


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_share_never_exceeds_the_task_budget(harness: StageHarness) -> None:  # noqa: F811
    spending_agent(harness, "big")
    one_round(harness, goal("big", "sub_agent", tools=["repo_grep"], budget={"usd": 10.0}), deliver(after=["big"]))
    inp, _ = await harness.run(budget=task_pb2.Budget(usd_micros=30_000))
    assert harness.script.review(inp.task_id, 1).stage_results["big"].failure_class == "denied"
    steps = [c for c in harness.script.calls if c.activity == names.TOOL_LOOP_STEP and c.workflow_id.endswith("big")]
    assert len(steps) <= 2, "Node 写的 10 美元不能让它花过 Task 的 0.03（budget_check 先拒、或子工作流按 Task 的停）"


async def test_sub_agent_steps_use_the_mid_tier(tmp_path: Path) -> None:
    gateway = ContextGateway(steps=[answered(200_000)] * 4)
    loop = ModelToolLoop(gateway, ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=100_000)), sub_agent_tier=SUB_AGENT_TIER)
    isolated = step_input([])
    isolated.isolated = True
    await loop.step(isolated)
    await loop.step(step_input([]))
    tiers = [r.call.tier for r in gateway.requests if r.call.role == enums_pb2.ROLE_ACT]
    assert tiers == [SUB_AGENT_TIER, TOOL_LOOP_TIER], "子 Agent 用 mid 档，主流程不换"
    assert SUB_AGENT_TIER.startswith("mid@")

    plain = ModelToolLoop(gateway, ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=100_000)))
    gateway.requests.clear()
    await plain.step(isolated)
    assert [r.call.tier for r in gateway.requests if r.call.role == enums_pb2.ROLE_ACT] == [TOOL_LOOP_TIER], "开关关着时不换"
