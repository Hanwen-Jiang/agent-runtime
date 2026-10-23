"""S-09：Task 工作流把上一轮（Stage、结果、Verdict）与 Task Plan 交给下一轮的 Planner。

replan 要“只重做剩余部分，保留已验证的 Node”（计划 S-09 验收），Planner 就得知道上一轮做了什么、Reviewer 保留了哪些 Node。
这只改 activity 的输入，不改命令序列；已有的 golden 事件历史照样回放（test_replay、test_stage_replay）。
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentruntime.common.v1 import enums_pb2
from agentruntime.workflow.v1 import task_pb2

from agentruntime_worker import names
from agentruntime_worker.activities.stubs import StubActivities
from agentruntime_worker.activities.types import PlanRoundInput, PlanRoundOutput, ReviewRoundInput
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.worker import QUEUE_COMPONENTS, build_workers

from .stage_harness import Script
from .support import task_input

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

FIRST_PLAN = {"triage": {"level": "full", "task_family": "analytics.attribution"}, "horizon": "per_stage"}
REPLAN = {"verdict": "replan", "reason": "假设被证伪", "replan": {"keep_nodes": ["answer"], "stuck_reason": "换个角度回答"}}


class ReplanOnceStubs(StubActivities):
    """第一轮的 Planner 给出 Task Plan，Reviewer 判 replan；第二轮照桩执行。记下 Planner 收到的输入。"""

    def __init__(self, config: WorkerConfig) -> None:
        super().__init__(config)
        self.plan_inputs: list[PlanRoundInput] = []

    @activity.defn(name=names.PLAN_ROUND)
    async def plan_round(self, inp: PlanRoundInput) -> PlanRoundOutput:
        self.plan_inputs.append(inp)
        out = await super().plan_round(inp)
        if inp.round_seq == 1:
            out.task_plan = FIRST_PLAN
        return out

    @activity.defn(name=names.REVIEW_ROUND)
    async def review_round(self, inp: ReviewRoundInput) -> dict[str, Any]:
        if inp.round_seq == 1:
            return REPLAN
        return await super().review_round(inp)


async def test_the_next_round_sees_the_last_round_and_the_task_plan(temporal_env: WorkflowEnvironment, tmp_path: Path) -> None:
    config = WorkerConfig(call_log=tmp_path / "calls.jsonl", stub_node_timeout_s=20)
    stubs = ReplanOnceStubs(config)
    client = temporal_env.client
    inp: task_pb2.TaskWorkflowInput = task_input(stub_plan="answer")
    events = Script()  # T-11b：进度事件的替身（真实的在 Go 的 Session Worker）
    async with contextlib.AsyncExitStack() as stack:
        for worker in [*build_workers(client, config, list(QUEUE_COMPONENTS), stubs=stubs), Worker(client, task_queue=names.SESSION_QUEUE, activities=[events.publish_task_event])]:
            await stack.enter_async_context(worker)
        handle = await client.start_workflow(names.TASK_WORKFLOW, inp, id=inp.task_id, task_queue=names.TASK_QUEUE, result_type=task_pb2.TaskWorkflowResult)
        result = await asyncio.wait_for(handle.result(), timeout=60)

    assert result.status == enums_pb2.TASK_STATUS_DONE and result.rounds == 2
    first, second = [p for p in stubs.plan_inputs if p.task_id == inp.task_id]
    assert first.last_round is None and first.task_plan is None
    assert second.task_plan == FIRST_PLAN
    last = second.last_round
    assert last is not None
    assert last["verdict"] == REPLAN
    assert [n["id"] for n in last["stage"]["nodes"]] == ["answer"]
    assert last["results"]["answer"]["status"] == "succeeded" and last["results"]["answer"]["role"] == "deliver"
