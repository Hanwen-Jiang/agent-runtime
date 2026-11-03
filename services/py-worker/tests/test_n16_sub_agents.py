"""docs/plan/feedback-2026-11-01.md N-16：并行的子 Agent（附录 E.1；人工验收基线 Q74；契约清单 n15_n16_contract_cases.json 的 N-16 部分）。

- 分工：Planner 的规则 11，一个 Stage 最多 sp.MAX_SUB_AGENTS 个 sub_agent，各自只读、互不重叠；超出的 Stage 被拒绝。
- 权限：默认继承、不能提升——sub_agent 的 effect 上限不高于 read 也不高于 Task 的；sub_task 不高于父 Task；子 Task 的预算不超过剩下的。
- 合并：结果按 Node id 排序，与完成的先后无关。
- 进度：顶层 sub_agent 的 node_started 带 mode，它在子工作流里的工具调用以它的 node_id 推 tool_status。
- 取消：取消父 Task 时正在跑的子 Agent 一起取消。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from temporalio.client import WorkflowExecutionStatus

from agentruntime.common.v1 import enums_pb2
from agentruntime.workflow.v1 import task_pb2

from agentruntime_worker import names
from agentruntime_worker.activities.types import ToolLoopStep
from agentruntime_worker.planner import validate
from agentruntime_worker.workflows import stage_plan as sp

from .planner_support import context
from .stage_harness import Hold, StageHarness, harness  # noqa: F401 — harness 是 fixture
from .support import assert_valid
from .test_stage_runner import deliver, goal, one_round, scripted_steps, stage

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]


def agent(node_id: str, **extra: Any) -> dict[str, Any]:
    return goal(node_id, "sub_agent", tools=["repo_grep"], **extra)


def scripted_agents(h: StageHarness, *ids: str) -> None:
    h.script.tools["repo_grep"] = "read"
    for nid in ids:
        h.script.tool_steps[nid] = scripted_steps(
            ToolLoopStep(action="tool", tool="repo_grep", args={"pattern": nid, "path": "src"}),
            ToolLoopStep(action="finish", result={"found": nid}),
        )
    h.script.tool_results["repo_grep"] = lambda i: {"matches": []}


# --- 并行、合并与进度 ---


async def test_parallel_sub_agents_run_concurrently_and_merge_by_node_id(harness: StageHarness) -> None:
    scripted_agents(harness, "scan_b", "scan_a")
    hold = Hold()
    harness.script.holds[f"{names.TOOL_LOOP_STEP}:scan_b.1"] = hold
    one_round(harness, agent("scan_b"), agent("scan_a"), deliver(after=["scan_a", "scan_b"]))
    inp, handle = await harness.start()
    await harness.wait_for(lambda: any(c.workflow_id.endswith("scan_a") and c.activity == names.CALL_TOOL for c in harness.script.calls), "scan_a 调了工具")
    assert not hold.release.is_set(), "scan_b 停着时 scan_a 照样跑完了它的工具调用：两个子 Agent 并行"
    hold.release.set()
    await asyncio.wait_for(handle.result(), timeout=60)
    results = harness.script.review(inp.task_id, 1).stage_results
    assert list(results) == ["answer", "scan_a", "scan_b"], "按 Node id 排序合并，与完成的先后无关"
    assert results["scan_a"].result == {"found": "scan_a"} and results["scan_b"].result == {"found": "scan_b"}


async def test_sub_agents_publish_their_own_progress(harness: StageHarness) -> None:
    scripted_agents(harness, "scan_a", "scan_b")
    one_round(harness, agent("scan_a"), agent("scan_b"), deliver(after=["scan_a", "scan_b"]))
    inp, _ = await harness.run()
    events = harness.script.progress(inp.task_id)
    started = {e["node_id"]: e for e in events if e["event"] == "node_started"}
    assert started["scan_a"]["mode"] == "sub_agent" and started["scan_b"]["mode"] == "sub_agent"
    assert "mode" not in started["answer"], "Atomic Node 没有 mode"
    tools = [e for e in events if e["event"] == "tool_status"]
    assert {(e["node_id"], e["state"]) for e in tools} == {("scan_a", "running"), ("scan_a", "succeeded"), ("scan_b", "running"), ("scan_b", "succeeded")}
    for e in events:
        assert_valid("task_event_payload", e)
    origin = {k: wid for k, e, wid in harness.script.published if e.task_id == inp.task_id and "/tool_status/" in k}
    assert all(wid == sp.subworkflow_id(inp.task_id, 1, k.split("/")[2]) for k, wid in origin.items()), "子 Agent 的进度由它自己的子工作流推送"
    node_events = [e["event"] for e in events if e.get("node_id") == "scan_a"]
    assert node_events[0] == "node_started" and node_events[-1] == "node_finished", "子 Agent 的进度在它的 node_started 与 node_finished 之间"


# --- 分工：个数上限 ---


async def test_more_sub_agents_than_the_cap_are_rejected(harness: StageHarness) -> None:
    ids = [f"scan_{i}" for i in range(sp.MAX_SUB_AGENTS + 1)]
    scripted_agents(harness, *ids)
    one_round(harness, *(agent(i) for i in ids), deliver(after=ids))
    inp, _ = await harness.run()
    results = harness.script.review(inp.task_id, 1).stage_results
    assert all(r.status == "skipped" and "sub_agent" in (r.error or "") for r in results.values())
    assert not [c for c in harness.script.calls if c.activity == names.TOOL_LOOP_STEP], "一个子 Agent 也不派生"


def test_the_planner_sends_back_too_many_sub_agents() -> None:
    nodes = [goal(f"scan_{i}", "sub_agent", effect_ceiling="read") for i in range(sp.MAX_SUB_AGENTS + 1)]
    found = validate.problems(stage(1, *nodes), {"triage": {"level": "full"}, "horizon": "per_stage"}, level="replan", context=context(), keep_nodes=[])
    assert any("sub_agent" in p and "上限" in p for p in found)


# --- 权限：默认继承、不能提升（Q74） ---


async def test_a_sub_agent_cannot_raise_its_effect_ceiling(harness: StageHarness) -> None:
    scripted_agents(harness, "scan_a")
    harness.script.tools["repo_edit"] = "write_reversible"
    one_round(harness, agent("scan_a", effect_ceiling="write_reversible"), goal("scan_b", "sub_agent", tools=["repo_edit"]), deliver(after=["scan_a", "scan_b"]))
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_WRITE_REVERSIBLE)
    results = harness.script.review(inp.task_id, 1).stage_results
    assert results["scan_a"].failure_class == "denied" and "只读" in results["scan_a"].error
    assert results["scan_b"].failure_class == "denied" and "repo_edit" in results["scan_b"].error, "写工具不能交给子 Agent"


async def test_a_sub_agent_inherits_a_lower_task_ceiling(harness: StageHarness) -> None:
    harness.script.tool_steps["think"] = scripted_steps(ToolLoopStep(action="finish", result={"idea": "x"}))
    one_round(harness, goal("think", "sub_agent"), deliver(after=["think"]))
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_NONE)
    results = harness.script.review(inp.task_id, 1).stage_results
    assert results["think"].status == "succeeded", "没写 effect_ceiling 时继承 Task 的 none，不因缺省的 read 被拒"
    scripted_agents(harness, "scan_a")
    one_round(harness, agent("scan_a"), deliver(after=["scan_a"]), scenario="read_tool")
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_NONE, scenario="read_tool")
    assert harness.script.review(inp.task_id, 1).stage_results["scan_a"].failure_class == "denied", "继承来的 none 之下读工具也不给"


async def test_a_sub_task_cannot_exceed_the_parent_ceiling_or_budget(harness: StageHarness) -> None:
    s = harness.script
    s.plans["child"] = [stage(1, deliver("answer"))]
    s.deliver["child"] = "answer"
    one_round(harness, goal("nested", "sub_task", effect_ceiling="write_reversible"), deliver(after=["nested"]))
    inp, _ = await harness.run()
    assert s.review(inp.task_id, 1).stage_results["nested"].failure_class == "denied", "子 Task 的 effect 上限不能高于父 Task"

    one_round(harness, goal("nested", "sub_task", budget={"usd": 5.0}, inputs={"scenario": "child"}), deliver(after=["nested"]), scenario="budget")
    inp, _ = await harness.run(scenario="budget", budget=task_pb2.Budget(usd_micros=1_000_000))
    child = [p for p in s.plan_inputs if p.task_id != inp.task_id and p.round_seq == 1 and (p.task_state.get("constraints") or {}).get("budget_usd")]
    assert child and child[-1].task_state["constraints"]["budget_usd"] == 1.0, "子 Task 的预算压到父 Task 剩下的 1 美元，不是节点写的 5 美元"


# --- 取消传到子 Agent ---


async def test_cancelling_the_task_cancels_running_sub_agents(harness: StageHarness) -> None:
    scripted_agents(harness, "scan_a")
    hold = Hold()
    harness.script.holds[f"{names.TOOL_LOOP_STEP}:scan_a.1"] = hold
    one_round(harness, agent("scan_a"), deliver(after=["scan_a"]))
    inp, handle = await harness.start()
    await asyncio.wait_for(hold.entered.wait(), timeout=30)
    await handle.cancel()
    with pytest.raises(Exception):
        await asyncio.wait_for(handle.result(), timeout=60)
    child = harness.client.get_workflow_handle(sp.subworkflow_id(inp.task_id, 1, "scan_a"))
    await harness.wait_for(lambda: True, "noop")
    for _ in range(100):
        if (await child.describe()).status != WorkflowExecutionStatus.RUNNING:
            break
        await asyncio.sleep(0.1)
    assert (await child.describe()).status in (WorkflowExecutionStatus.CANCELED, WorkflowExecutionStatus.TERMINATED)
