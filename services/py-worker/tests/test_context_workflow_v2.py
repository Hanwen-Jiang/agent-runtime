"""Q87 / Q93：真实 Temporal Task → Stage → ContextBuilder Activity，包含新历史重放。"""

from __future__ import annotations

import contextlib
import dataclasses
import json
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from agentruntime.common.v1 import enums_pb2
from agentruntime_worker import names
from agentruntime_worker.activities.types import NodeInput, StagePolicy, StageResult, ToolLoopStepInput
from agentruntime_worker.context.builder import ContextBuilder, ContextConfig
from agentruntime_worker.context.partitions import STATE_HEADER
from agentruntime_worker.executor.tool_loop import ModelToolLoop, ToolLoopActivities
from agentruntime_worker.workflows import patches
from agentruntime_worker.workflows.stage_runner import SubAgentInput, SubAgentWorkflow, SubgraphWorkflow
from agentruntime_worker.workflows.task import TaskWorkflow

from .context_support import ContextGateway, TASK_PLAN, request, text_of, tool_call
from .executor_harness import make_fixture, model
from .history_split import first_task_patches
from .stage_harness import Script, StageHarness, returns
from .test_stage_runner import atomic, goal, stage

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]


async def test_task_passes_stable_snapshots_to_real_context_activities(temporal_env: WorkflowEnvironment, tmp_path: Path) -> None:
    script = Script(task_plan=TASK_PLAN)
    script.plans["main"] = [
        stage(1, atomic("previous")),
        stage(2, atomic("upstream"), atomic("unrelated"),
              goal("loop", "tool_loop", after=["upstream"], tools=["repo_read"], effect_ceiling="read", output_schema={"type": "object"}),
              model("answer", after=["loop"], role="deliver")),
    ]
    script.deliver["main"] = "answer"
    script.behaviors["previous"] = returns({"fact": "previous round fact"})
    script.behaviors["upstream"] = returns({"fact": "ancestor result"})
    script.behaviors["unrelated"] = returns({"fact": "unrelated sibling result"})
    script.tool_results["repo_read"] = lambda _: {"content": "actual tool observation"}
    fx = make_fixture(tmp_path)
    fx.models.reply({"summary": "done"})
    fx.executor.deps = dataclasses.replace(fx.executor.deps, context_builder=ContextBuilder(fx.models, fx.objects))
    gateway = ContextGateway(steps=[tool_call("repo_read", path="README.md"), {"ok": True}])
    loop = ToolLoopActivities(ModelToolLoop(gateway, ContextBuilder(gateway, fx.objects, ContextConfig(probe=False))))
    by_queue = script.by_queue()
    by_queue[names.MODEL_QUEUE] = [fx.executor.execute_model_node, loop.tool_loop_step]
    workflows = [TaskWorkflow, SubAgentWorkflow, SubgraphWorkflow]
    workers = [Worker(temporal_env.client, task_queue=names.TASK_QUEUE, workflows=workflows, activities=[script.run_hooks]),
               *(Worker(temporal_env.client, task_queue=q, activities=activities) for q, activities in by_queue.items())]
    harness = StageHarness(temporal_env.client, script)
    async with contextlib.AsyncExitStack() as stack:
        for worker in workers:
            await stack.enter_async_context(worker)
        inp, result = await harness.run(extra_inputs={"facts": ["fixed exact state"]})
        assert result.status == enums_pb2.TASK_STATUS_DONE, result
        history = await temporal_env.client.get_workflow_handle(inp.task_id).fetch_history()

    seen: list[NodeInput | ToolLoopStepInput] = []
    for event in history.events:
        scheduled = event.activity_task_scheduled_event_attributes
        expected = {names.EXECUTE_MODEL_NODE: NodeInput, names.TOOL_LOOP_STEP: ToolLoopStepInput}.get(scheduled.activity_type.name)
        if expected is not None:
            snapshot, = await temporal_env.client.data_converter.decode(scheduled.input.payloads, [expected])
            seen.append(snapshot)
    assert len(seen) == 3
    assert all(s.context_version == 2 and s.task_plan == TASK_PLAN for s in seen)
    assert all(s.task_family == TASK_PLAN["triage"]["task_family"] for s in seen)
    assert [s["node_id"] for s in seen[0].history] == ["previous", "upstream"]
    assert seen[1].history[-1]["observation"] == {"content": "actual tool observation"}
    assert [s["node_id"] for s in seen[-1].history] == ["previous", "upstream", "loop"]
    assert "RATIONALE-" not in json.dumps([dataclasses.asdict(s) for s in seen])
    for request in [*gateway.of("step"), *fx.models.requests]:
        assert request.call.task_family == TASK_PLAN["triage"]["task_family"]
        text = text_of(request.generation.messages)
        assert "previous round fact" in text and "ancestor result" in text
        assert "unrelated sibling result" not in text
        precise = next(m.text for m in request.generation.messages if m.text.startswith(STATE_HEADER))
        assert json.loads(precise.split("\n", 1)[1])["task_id"] == inp.task_id
    first, later = first_task_patches(list(history.events))
    assert patches.CONTEXT_BUILDER_V2_PATCH in first and not later
    await Replayer(workflows=workflows).replay_workflow(history)


@pytest.mark.parametrize("version", [1, 2])
async def test_child_inherits_parent_version_even_on_a_new_worker(temporal_env: WorkflowEnvironment, tmp_path: Path, version: int) -> None:
    req = request([])
    fx = make_fixture(tmp_path)
    gateway = ContextGateway(steps=[{"ok": True}])
    loop = ToolLoopActivities(ModelToolLoop(gateway, ContextBuilder(gateway, fx.objects)))
    script = Script()
    inp = dataclasses.asdict(SubAgentInput(tenant=req.tenant, task_id=req.task_id, round_seq=1, node={**req.node, "tools": []},
                                          effect_ceiling="read", tools={}, policy=StagePolicy(), context_version=version,
                                          task_family=TASK_PLAN["triage"]["task_family"]))
    if version == 1:
        del inp["context_version"]  # 尚未升级的父工作流历史里的子工作流输入。
    async with Worker(temporal_env.client, task_queue=names.TASK_QUEUE, workflows=[SubAgentWorkflow], activities=[script.run_hooks]), \
               Worker(temporal_env.client, task_queue=names.MODEL_QUEUE, activities=[loop.tool_loop_step]):
        handle = await temporal_env.client.start_workflow(names.SUB_AGENT_WORKFLOW, inp, id=f"context-child-{version}",
                                                         task_queue=names.TASK_QUEUE, result_type=StageResult)
        assert (await handle.result()).status == "succeeded"
        history = await handle.fetch_history()
    scheduled = next(e.activity_task_scheduled_event_attributes for e in history.events
                     if e.activity_task_scheduled_event_attributes.activity_type.name == names.TOOL_LOOP_STEP)
    step, = await temporal_env.client.data_converter.decode(scheduled.input.payloads, [ToolLoopStepInput])
    assert step.context_version == version
    assert step.isolated and not step.task_state and step.task_plan is None
    assert gateway.requests[-1].call.task_family == TASK_PLAN["triage"]["task_family"]
    first, _ = first_task_patches(list(history.events))
    assert patches.CONTEXT_BUILDER_V2_PATCH in first, "新 Worker 已支持 v2，子工作流仍按父输入的版本运行"
    await Replayer(workflows=[SubAgentWorkflow]).replay_workflow(history)
