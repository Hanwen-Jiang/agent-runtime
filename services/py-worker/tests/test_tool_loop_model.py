"""Context Builder 的第一个使用者：经 Model Gateway 的 tool_loop 一步（附录 E.1），AR_TOOL_LOOP=model 时代替桩。

- 上下文由 Context Builder 组装（分区与缓存断点、Compaction Ladder）；模型的 rationale 只写入 trace，不进下一步的上下文。
- Stage Runner 把 Task State 与 Task Plan 交给每一步（只改 activity 的输入，不改命令序列；已有的 golden 照样回放）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentruntime_worker import names
from agentruntime_worker.activities.types import ToolLoopStep, ToolLoopStepInput
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.context.builder import ContextBuilder
from agentruntime_worker.executor.tool_loop import TOOL_LOOP_TIER, ModelToolLoop, ToolLoopActivities
from agentruntime_worker.worker import decision_activities

from .context_support import GOAL_NODE, TASK_ID, TASK_PLAN, TASK_STATE, TENANT, ContextGateway, config, objects, read_step, text_of, tool_call
from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .test_stage_runner import deliver, goal, one_round, scripted_steps


def step_input(history: list[dict], iteration: int | None = None) -> ToolLoopStepInput:
    i = iteration or len(history) + 1
    return ToolLoopStepInput(
        tenant=TENANT,
        task_id=TASK_ID,
        round_seq=1,
        node=GOAL_NODE,
        iteration=i,
        history=history,
        step_id=f"fix.{2 * i - 1}",
        task_state=TASK_STATE,
        task_plan=TASK_PLAN,
    )


def loop(tmp_path: Path, gateway: ContextGateway) -> ModelToolLoop:
    return ModelToolLoop(gateway, ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=100_000)))


async def test_a_tool_call_becomes_a_tool_step(tmp_path: Path) -> None:
    gateway = ContextGateway(steps=[tool_call("repo_read", path="src/checkout.py")])
    step = await loop(tmp_path, gateway).step(step_input([]))
    assert step == ToolLoopStep(action="tool", tool="repo_read", args={"path": "src/checkout.py"})
    [request] = gateway.requests
    assert request.call.node_id == "fix.1" and request.call.tier == TOOL_LOOP_TIER
    assert request.tenant.tenant_id == TENANT.tenant_id
    assert [t.name for t in request.generation.tools] == GOAL_NODE["tools"]
    assert request.generation.messages[0].cache_breakpoint, "上下文经 Context Builder 组装"


async def test_tool_definitions_come_from_the_registrations(tmp_path: Path) -> None:
    """T-10：给模型的工具定义（说明与参数 schema）取自 ToolDriver 的登记；Goal Node 里写了、但没登记的工具不给模型。"""
    from datetime import timedelta

    from google.protobuf import json_format

    from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
    from agentruntime_core.enums import Effect
    from agentruntime_core.providers.tool_driver import ToolInvocation, ToolSpec

    from agentruntime_worker.tools import ToolCatalog

    async def handler(invocation: ToolInvocation) -> dict:
        return {}

    provider = LocalToolDriverProvider()
    schema = {"type": "object", "required": ["path"], "properties": {"path": {"type": "string", "description": "相对工作区的路径"}}}
    provider.register(ToolSpec(name="repo_read", version="v1", description="读工作区里的文件", input_schema=schema, output_schema={"type": "object"},
                               effect=Effect.READ, permissions=frozenset(), timeout=timedelta(seconds=5)), handler)
    gateway = ContextGateway(steps=[tool_call("repo_read", path="src/checkout.py")])
    tool_loop = ModelToolLoop(gateway, ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=100_000)), tools=ToolCatalog(provider))
    await tool_loop.step(step_input([]))
    [request] = gateway.requests
    tools = {t.name: t for t in request.generation.tools}
    assert set(tools) == {"repo_read"}, "没登记的 run_tests、repo_edit、web_search 不给模型"
    assert tools["repo_read"].description == "读工作区里的文件"
    assert json_format.MessageToDict(tools["repo_read"].params_schema.value) == schema


async def test_a_result_without_tool_calls_finishes(tmp_path: Path) -> None:
    gateway = ContextGateway(steps=[{"patch": "fix tz"}])
    step = await loop(tmp_path, gateway).step(step_input([read_step(0)]))
    assert step.action == "finish" and step.result == {"patch": "fix tz"}


async def test_rationale_never_reaches_the_next_step(tmp_path: Path) -> None:
    gateway = ContextGateway(steps=[tool_call("repo_read", path="a.py"), tool_call("run_tests", target="make test")])
    runner = loop(tmp_path, gateway)
    first = await runner.step(step_input([]))
    await runner.step(step_input([{"tool": first.tool, "args": first.args, "observation": {"content": "print('a')"}}]))
    assert "模型的推理过程" not in text_of(list(gateway.requests[1].generation.messages))


def test_the_worker_switch() -> None:
    assert WorkerConfig.from_env({"AR_TOOL_LOOP": "model"}).tool_loop == "model"
    assert WorkerConfig.from_env({}).tool_loop == "stub"
    assert WorkerConfig.from_env({"AR_CONTEXT_EFFECTIVE_TOKENS": "48000"}).context_effective_tokens == 48_000
    with pytest.raises(ValueError):
        WorkerConfig.from_env({"AR_TOOL_LOOP": "magic"})
    with pytest.raises(ValueError):
        WorkerConfig.from_env({"AR_CONTEXT_EFFECTIVE_TOKENS": "0"})


def test_model_tool_loop_replaces_the_stub(tmp_path: Path) -> None:
    activities = decision_activities(WorkerConfig(tool_loop="model", data_dir=tmp_path))
    bound = activities[names.TOOL_LOOP_STEP]
    assert isinstance(getattr(bound, "__self__", None), ToolLoopActivities)


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_the_stage_runner_hands_the_task_state_and_plan_to_each_step(harness: StageHarness) -> None:  # noqa: F811
    s = harness.script
    s.task_plan = {**TASK_PLAN, "context": {"work_mode": "research", "mask_at": 0.3}}
    s.tools.update({"web_search": "read"})
    s.tool_steps["dig"] = scripted_steps(ToolLoopStep(action="tool", tool="web_search", args={"q": "x"}), ToolLoopStep(action="finish", result={}))
    one_round(harness, goal("dig", "tool_loop", tools=["web_search"]), deliver(after=["dig"]))
    inp, _ = await harness.run()
    steps = [c for c in s.calls if c.activity == names.TOOL_LOOP_STEP and c.workflow_id == inp.task_id]
    assert len(steps) == 2
    assert all(c.inp.task_plan == s.task_plan for c in steps)
    assert all(c.inp.task_state.get("goal") for c in steps), "带上 Task State"
