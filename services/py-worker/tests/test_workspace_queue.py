"""WS-READ-02：Workspace 操作单独一个队列（docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 2 节）。

1. task-service 新起的操作在 workspace 队列：工作流的 activity 也排在 workspace 队列，只有 workspace 池的 Worker 就能跑完。
2. 改之前起在 task 队列的操作照旧：activity 排在 program 队列（命令序列不变，旧历史回放不受影响，不用版本分支）。
"""

from __future__ import annotations

from typing import Any

import pytest
from google.protobuf import json_format, struct_pb2
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentruntime.common.v1 import tenant_pb2
from agentruntime.workflow.v1 import workspace_pb2
from agentruntime_worker import names
from agentruntime_worker.workflows.workspace import WorkspaceOperationWorkflow, operation_workflow_id

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]


@activity.defn(name=names.WORKSPACE_OPERATION)
async def report_queue(inp: workspace_pb2.WorkspaceOperationInput) -> workspace_pb2.WorkspaceOperationResult:
    """假的 workspace_operation：结果里带上它在哪个队列上跑的。"""
    result = struct_pb2.Struct()
    result.update({"queue": activity.info().task_queue})
    return workspace_pb2.WorkspaceOperationResult(result=result)


def operation(op_id: str) -> workspace_pb2.WorkspaceOperationInput:
    return workspace_pb2.WorkspaceOperationInput(
        tenant=tenant_pb2.TenantContext(tenant_id="t1", user_id="u1"), operation_id=op_id,
        operation=workspace_pb2.WORKSPACE_OPERATION_LIST_FILES, workspace_id="w1",
    )


async def run_on(client: Client, workflow_queue: str, op_id: str) -> dict[str, Any]:
    out = await client.execute_workflow(
        names.WORKSPACE_OPERATION_WORKFLOW, operation(op_id), id=operation_workflow_id(op_id), task_queue=workflow_queue,
        result_type=workspace_pb2.WorkspaceOperationResult,
    )
    assert not out.HasField("error"), out
    return json_format.MessageToDict(out.result)


async def connect(env: WorkflowEnvironment) -> Client:
    # 队列名是固定的：用单独的连接，与别的用例在同一个客户端上登记的 Worker 不冲突。
    return await Client.connect(env.client.service_client.config.target_host, namespace=env.client.namespace)


async def test_new_operations_run_entirely_on_the_workspace_queue(temporal_env: WorkflowEnvironment) -> None:
    client = await connect(temporal_env)
    async with Worker(client, task_queue=names.WORKSPACE_QUEUE, workflows=[WorkspaceOperationWorkflow], activities=[report_queue]):
        result = await run_on(client, names.WORKSPACE_QUEUE, "wso-wsread02queue000000000001")
    assert result == {"queue": names.WORKSPACE_QUEUE}


async def test_operations_started_on_the_task_queue_keep_the_program_queue(temporal_env: WorkflowEnvironment) -> None:
    client = await connect(temporal_env)
    async with (
        Worker(client, task_queue=names.TASK_QUEUE, workflows=[WorkspaceOperationWorkflow]),
        Worker(client, task_queue=names.PROGRAM_QUEUE, activities=[report_queue]),
    ):
        result = await run_on(client, names.TASK_QUEUE, "wso-wsread02queue000000000002")
    assert result == {"queue": names.PROGRAM_QUEUE}


class PersistedFailure:
    def __init__(self):
        self.attempts = 0
        self.saved = None

    @activity.defn(name=names.WORKSPACE_OPERATION)
    async def fail(self, inp: workspace_pb2.WorkspaceOperationInput) -> workspace_pb2.WorkspaceOperationResult:
        from temporalio.exceptions import ApplicationError
        raise ApplicationError("network_timeout: simulated timeout", non_retryable=True)

    @activity.defn(name=names.WORKSPACE_OPERATION_RESULT)
    async def save(self, inp: workspace_pb2.WorkspaceOperationInput, result: workspace_pb2.WorkspaceOperationResult) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("simulated storage restart")
        self.saved = result


async def test_async_final_failure_is_saved_before_completion_and_replays(temporal_env: WorkflowEnvironment) -> None:
    from temporalio.worker import Replayer
    client = await connect(temporal_env)
    activities = PersistedFailure()
    inp = operation("wso-async-final-failure")
    inp.args.update({"__async_result": True})
    async with Worker(client, task_queue=names.WORKSPACE_QUEUE, workflows=[WorkspaceOperationWorkflow], activities=[activities.fail, activities.save]):
        handle = await client.start_workflow(names.WORKSPACE_OPERATION_WORKFLOW, inp, id=operation_workflow_id(inp.operation_id), task_queue=names.WORKSPACE_QUEUE, result_type=workspace_pb2.WorkspaceOperationResult)
        result = await handle.result()
        assert result.HasField("error") and result.error.code == "network_timeout"
        assert activities.attempts == 2 and activities.saved == result
        history = await handle.fetch_history()
    await Replayer(workflows=[WorkspaceOperationWorkflow]).replay_workflow(history)


async def test_legacy_root_golden_replay(temporal_env: WorkflowEnvironment) -> None:
    import os
    from pathlib import Path
    from temporalio.client import WorkflowHistory
    from temporalio.worker import Replayer
    path = Path(__file__).parent / "histories" / "workspace_root_legacy.json"
    if os.environ.get("UPDATE_WORKSPACE_GOLDEN") == "1":
        client = await connect(temporal_env)
        async with (
            Worker(client, task_queue=names.TASK_QUEUE, workflows=[WorkspaceOperationWorkflow]),
            Worker(client, task_queue=names.PROGRAM_QUEUE, activities=[report_queue]),
        ):
            await run_on(client, names.TASK_QUEUE, "wso-legacy-root-golden")
            history = await client.get_workflow_handle(operation_workflow_id("wso-legacy-root-golden")).fetch_history()
            path.write_text(history.to_json())
    history = WorkflowHistory.from_json("wsop-wso-legacy-root-golden", path.read_text())
    await Replayer(workflows=[WorkspaceOperationWorkflow]).replay_workflow(history)
