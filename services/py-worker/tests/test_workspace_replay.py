"""Replay unmodified baseline Workspace histories with today's contracts.

Fixtures were recorded by real Temporal using the immutable 67b4f5bc source,
not synthesized or regenerated from the implementation being tested.
"""
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from agentruntime_worker import names
from agentruntime_worker.workflows.workspace import WorkspaceOperationWorkflow


@pytest.mark.parametrize("name,queue,attempts", [
    ("workspace-queue", names.WORKSPACE_QUEUE, 3),
    ("legacy-task-queue", names.PROGRAM_QUEUE, 3),
    ("legacy-push-failure", names.PROGRAM_QUEUE, 1),
])
async def test_baseline_workspace_histories_replay(name, queue, attempts):
    raw = (Path(__file__).parent / "workspace_histories" / f"{name}.json").read_text()
    history = WorkflowHistory.from_json(name, raw)
    scheduled = [e.activity_task_scheduled_event_attributes for e in history.events if e.HasField("activity_task_scheduled_event_attributes")]
    assert len(scheduled) == 1
    assert scheduled[0].task_queue.name == queue
    assert scheduled[0].retry_policy.maximum_attempts == attempts
    result = await Replayer(workflows=[WorkspaceOperationWorkflow]).replay_workflow(history, raise_on_replay_failure=False)
    assert result.replay_failure is None
