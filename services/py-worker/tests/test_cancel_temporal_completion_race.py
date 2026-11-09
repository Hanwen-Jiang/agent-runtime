"""Real Temporal cancellation must win when its activity completes normally."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import uuid

import pytest
from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.exceptions import CancelledError as TemporalCancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from agentruntime_worker.workflows import interrupts
from agentruntime_worker.workflows.patches import CANCEL_COMPLETION_RACE_PATCH


@workflow.defn
class CancelCompletedActivityRace:
    @workflow.run
    async def run(self) -> str:
        workflow.patched(CANCEL_COMPLETION_RACE_PATCH)
        try:
            await interrupts.run([], workflow.execute_activity(
                "return_after_cancel", start_to_close_timeout=timedelta(seconds=30),
                heartbeat_timeout=timedelta(seconds=2),
                retry_policy=RetryPolicy(maximum_attempts=1),
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            ))
            await workflow.execute_activity("next_operation", start_to_close_timeout=timedelta(seconds=10))
            return "incorrectly continued"
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.create_task(workflow.execute_activity(
                "cancel_finalization", start_to_close_timeout=timedelta(seconds=10))))
            raise


class CompletionRaceActivities:
    def __init__(self):
        self.started = asyncio.Event()
        self.normal_return_after_cancel = asyncio.Event()
        self.next_calls = 0
        self.finalized = 0

    @activity.defn
    async def return_after_cancel(self) -> str:
        self.started.set()
        try:
            while True:
                activity.heartbeat()
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.normal_return_after_cancel.set()
            return "late normal result"

    @activity.defn
    async def next_operation(self) -> None:
        self.next_calls += 1

    @activity.defn
    async def cancel_finalization(self) -> None:
        self.finalized += 1


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_real_temporal_normal_activity_result_does_not_swallow_cancel(temporal_env: WorkflowEnvironment, tmp_path):
    activities = CompletionRaceActivities()
    queue = "cancel-normal-race-" + uuid.uuid4().hex
    async with Worker(
        temporal_env.client, task_queue=queue, workflows=[CancelCompletedActivityRace],
        activities=[activities.return_after_cancel, activities.next_operation, activities.cancel_finalization],
        workflow_runner=UnsandboxedWorkflowRunner(),
        max_heartbeat_throttle_interval=timedelta(milliseconds=50),
    ):
        handle = await temporal_env.client.start_workflow(CancelCompletedActivityRace.run, id=queue, task_queue=queue)
        await asyncio.wait_for(activities.started.wait(), timeout=10)
        await handle.cancel()
        failure = None
        try:
            await asyncio.wait_for(handle.result(), timeout=15)
        except WorkflowFailureError as error:
            failure = error
        history = await handle.fetch_history()
    (tmp_path / "cancel-normal-race-history.json").write_text(history.to_json())
    cancel = next(e.event_id for e in history.events if e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED)
    original = next(e.event_id for e in history.events if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED and e.activity_task_scheduled_event_attributes.activity_type.name == "return_after_cancel")
    completed = [e for e in history.events if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED and e.activity_task_completed_event_attributes.scheduled_event_id == original]
    assert activities.normal_return_after_cancel.is_set(), "activity never received the cancellation race"
    assert completed and completed[0].event_id > cancel, "must exercise normal activity completion AFTER workflow cancellation"
    assert activities.next_calls == 0, "parent cancellation was swallowed and a new operation ran"
    assert activities.finalized == 1
    assert failure is not None and isinstance(failure.cause, TemporalCancelledError)
    assert history.events[-1].event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED
    assert not any(e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED and e.activity_task_scheduled_event_attributes.activity_type.name == "next_operation" for e in history.events)
    await Replayer(workflows=[CancelCompletedActivityRace], workflow_runner=UnsandboxedWorkflowRunner()).replay_workflow(history)
