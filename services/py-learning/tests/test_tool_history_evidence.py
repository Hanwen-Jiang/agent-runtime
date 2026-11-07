"""Actual Temporal payloads and Claim-Check, without models or live writes."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from temporalio.api.common.v1 import ActivityType, Memo, Payloads, RetryPolicy, WorkflowExecution, WorkflowType
from temporalio.api.enums.v1 import RetryState, TimeoutType
from temporalio.api.failure.v1 import Failure, TimeoutFailureInfo
from temporalio.api.history.v1 import (
    ActivityTaskCanceledEventAttributes, ActivityTaskCompletedEventAttributes, ActivityTaskFailedEventAttributes,
    ActivityTaskScheduledEventAttributes, ActivityTaskStartedEventAttributes, ActivityTaskTimedOutEventAttributes,
    ChildWorkflowExecutionStartedEventAttributes, HistoryEvent, StartChildWorkflowExecutionInitiatedEventAttributes,
    WorkflowExecutionCompletedEventAttributes, WorkflowExecutionStartedEventAttributes,
)
from temporalio.client import WorkflowHistory
from temporalio.converter import DataConverter

from agentruntime_core.adapters.claim_check import ClaimCheck
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.tenancy import TenantContext
from agentruntime_learning.benchmarks import tool_history_evidence as tools
from agentruntime_worker.activities.types import NodeInput, Tenant, ToolCallInput, ToolLoopStepInput


async def begin(tenant="tenant1", *, parent=None, workflow_type=None):
    memo = (await DataConverter.default.encode([tenant]))[0]
    attrs = WorkflowExecutionStartedEventAttributes(memo=Memo(fields={"agentruntime_scope": memo}))
    if parent:
        attrs.parent_workflow_execution.CopyFrom(WorkflowExecution(workflow_id=parent[0], run_id=parent[1]))
    if workflow_type:
        attrs.workflow_type.name = workflow_type
    return HistoryEvent(event_id=1, workflow_execution_started_event_attributes=attrs)


async def scheduled(event_id, args, *, tool="read_file", task="task", tenant="tenant1", cc=None):
    value = ToolCallInput(tenant=Tenant(tenant_id=tenant), task_id=task, round_seq=1,
                          node_id=f"read.{event_id}", tool=tool, args=args, effect="read")
    payloads = await DataConverter.default.encode([value])
    if cc:
        payloads = await cc.bind(TenantContext(tenant_id=tenant)).encode(payloads)
    return HistoryEvent(event_id=event_id, activity_task_scheduled_event_attributes=ActivityTaskScheduledEventAttributes(
        activity_id=str(event_id), activity_type=ActivityType(name="call_tool"), input=Payloads(payloads=payloads),
        retry_policy=RetryPolicy(maximum_attempts=3)))


def started(event_id, scheduled_id, attempt=1, *, prior_failure=False):
    attrs = ActivityTaskStartedEventAttributes(scheduled_event_id=scheduled_id, attempt=attempt)
    if prior_failure:
        attrs.last_failure.CopyFrom(Failure(message="PRIVATE failure output"))
    return HistoryEvent(event_id=event_id, activity_task_started_event_attributes=attrs)


def completed(event_id, scheduled_id, started_id):
    return HistoryEvent(event_id=event_id, activity_task_completed_event_attributes=ActivityTaskCompletedEventAttributes(
        scheduled_event_id=scheduled_id, started_event_id=started_id))


def closed(event_id):
    return HistoryEvent(event_id=event_id, workflow_execution_completed_event_attributes=WorkflowExecutionCompletedEventAttributes())


async def test_full_arguments_ignore_object_order_but_preserve_tool_and_array_order():
    h = WorkflowHistory("task", [await begin(),
        await scheduled(2, {"path": "same", "options": {"b": 2, "a": 1}}), started(3, 2), completed(4, 2, 3),
        await scheduled(5, {"options": {"a": 1, "b": 2}, "path": "same"}), started(6, 5), completed(7, 5, 6),
        await scheduled(8, {"path": "same", "options": {"a": 1, "b": 2}}, tool="write_file"), started(9, 8), completed(10, 8, 9),
        closed(11)])
    ev = await tools.history_evidence(h, "tenant1", run_id="run1")
    report = tools.summarize([ev])
    assert report["complete"] and report["scheduled_tool_requests"] == 3
    assert report["repeated_full_requests"] == report["repeated_full_started_requests"] == 1
    assert report["reported_started_attempts_lower_bound"] == 3
    assert tools.canonical_request_hash("x", {"v": [1, 2]}) != tools.canonical_request_hash("x", {"v": [2, 1]})
    assert tools.canonical_request_hash("x", {"v": True}) != tools.canonical_request_hash("x", {"v": 1})


async def test_buffered_retry_counter_is_not_another_model_tool_decision():
    h = WorkflowHistory("task", [await begin(), await scheduled(2, {"path": "same"}),
        started(3, 2, attempt=3, prior_failure=True), completed(4, 2, 3), closed(5)])
    report = tools.summarize([await tools.history_evidence(h, "tenant1")])
    assert report["complete"]
    assert report["scheduled_tool_requests"] == 1 and report["repeated_full_requests"] == 0
    assert report["reported_started_attempts_lower_bound"] == 3
    assert report["reported_retry_attempts_lower_bound"] == 2
    assert len(report["workflows"][0]["calls"][0]["observed_started_events"]) == 1
    assert "PRIVATE" not in json.dumps(report)


async def test_claim_check_reads_real_encrypted_payloads_and_exports_no_arguments(tmp_path):
    cc = ClaimCheck(LocalDiskObjectStoreProvider(tmp_path / "objects", signing_key=b"test"),
                    LocalFileSecretStoreProvider(tmp_path / "secrets.json"), threshold=1)
    h = WorkflowHistory("task", [await begin(), await scheduled(2, {"token": "SECRET-payload-123", "nested": {"a": 1}}, cc=cc),
                                 started(3, 2), completed(4, 2, 3), closed(5)])
    report = await tools.history_evidence(h, "tenant1", claim_check=cc)
    assert report["complete"] and report["calls"][0]["claim_check_decoded"]
    raw = json.dumps(report)
    assert "SECRET-payload" not in raw and '"arguments"' not in raw and '"args"' not in raw and "obj://" not in raw
    missing = await tools.history_evidence(h, "tenant1")
    assert not missing["complete"] and missing["calls"][0]["input_state"] == "unresolved"
    assert missing["errors"][0]["error_type"] == "ValueError"
    with pytest.raises(ValueError, match="tenant"):
        await tools.history_evidence(h, "tenant2", claim_check=cc)


async def test_scheduled_open_not_started_and_missing_started_are_distinct():
    pending = WorkflowHistory("task", [await begin(), await scheduled(2, {})])
    result = await tools.history_evidence(pending, "tenant1")
    assert not result["complete"] and result["calls"][0]["state"] == "unresolved"
    assert result["calls"][0]["start_state"] == "not_observed"
    timeout = HistoryEvent(event_id=3, activity_task_timed_out_event_attributes=ActivityTaskTimedOutEventAttributes(
        scheduled_event_id=2, started_event_id=0, retry_state=RetryState.RETRY_STATE_NON_RETRYABLE_FAILURE,
        failure=Failure(timeout_failure_info=TimeoutFailureInfo(timeout_type=TimeoutType.TIMEOUT_TYPE_SCHEDULE_TO_START))))
    result = await tools.history_evidence(WorkflowHistory("task", [*pending.events, timeout, closed(4)]), "tenant1")
    assert result["complete"] and result["calls"][0]["start_state"] == "not_started"
    assert result["calls"][0]["reported_started_attempt"] == 0
    broken = await tools.history_evidence(WorkflowHistory("task", [*pending.events, completed(3, 2, 99), closed(4)]), "tenant1")
    assert not broken["complete"] and broken["calls"][0]["start_state"] == "unresolved"
    assert broken["errors"][-1]["error_type"] == "MissingStartedEvent"


async def test_failed_and_prestart_canceled_activities_keep_retry_evidence_without_messages():
    canceled_schedule = await scheduled(5, {"secret": "SECRET-argument"})
    canceled_schedule.activity_task_scheduled_event_attributes.retry_policy.maximum_attempts = 1
    history = WorkflowHistory("task", [
        await begin(), await scheduled(2, {"secret": "SECRET-argument"}),
        started(3, 2, attempt=2, prior_failure=True),
        HistoryEvent(event_id=4, activity_task_failed_event_attributes=ActivityTaskFailedEventAttributes(
            scheduled_event_id=2, started_event_id=3, retry_state=RetryState.RETRY_STATE_MAXIMUM_ATTEMPTS_REACHED,
            failure=Failure(message="SECRET-token from tool stderr"))),
        canceled_schedule,
        HistoryEvent(event_id=6, activity_task_canceled_event_attributes=ActivityTaskCanceledEventAttributes(
            scheduled_event_id=5, started_event_id=0)), closed(7),
    ])
    result = tools.summarize([await tools.history_evidence(history, "tenant1")])
    assert result["complete"] and result["not_started_activity_count"] == 1
    assert result["repeated_full_requests"] == 1 and result["repeated_full_started_requests"] == 0
    assert result["reported_retry_attempts_lower_bound"] == 1
    assert result["workflows"][0]["calls"][0]["retry_state"] == "RETRY_STATE_MAXIMUM_ATTEMPTS_REACHED"
    assert "SECRET" not in json.dumps(result)


async def test_argument_tenant_or_task_mismatch_is_not_decoded_as_valid():
    for kwargs in ({"tenant": "other"}, {"task": "other-task"}):
        history = WorkflowHistory("task", [await begin(), await scheduled(2, {"private": "SECRET"}, **kwargs),
                                            started(3, 2), completed(4, 2, 3), closed(5)])
        result = await tools.history_evidence(history, "tenant1")
        assert not result["complete"] and result["calls"][0]["input_state"] == "unresolved"
        assert "SECRET" not in json.dumps(result)


class FakeClient:
    namespace = "isolated-eval"

    def __init__(self, histories):
        self.histories = histories
        self.requests = []

    def get_workflow_handle(self, wid, run_id=None):
        self.requests.append((wid, run_id))
        current = run_id or next(run for name, run in self.histories if name == wid)
        async def describe():
            return SimpleNamespace(run_id=current)
        async def fetch_history():
            return self.histories[(wid, current)]
        return SimpleNamespace(describe=describe, fetch_history=fetch_history)


async def test_actual_child_run_and_parent_identity_are_retained():
    child_id = "task/r1/child"
    root = WorkflowHistory("task", [await begin(),
        HistoryEvent(event_id=2, start_child_workflow_execution_initiated_event_attributes=StartChildWorkflowExecutionInitiatedEventAttributes(
            workflow_id=child_id, workflow_type=WorkflowType(name="SubAgentWorkflow"))),
        HistoryEvent(event_id=3, child_workflow_execution_started_event_attributes=ChildWorkflowExecutionStartedEventAttributes(
            initiated_event_id=2, namespace="isolated-eval", workflow_type=WorkflowType(name="SubAgentWorkflow"),
            workflow_execution=WorkflowExecution(workflow_id=child_id, run_id="child-run"))), closed(4)])
    child = WorkflowHistory(child_id, [await begin(parent=("task", "root-run")), await scheduled(2, {"path": "README"}),
                                       started(3, 2), completed(4, 2, 3), closed(5)])
    client = FakeClient({("task", "root-run"): root, (child_id, "child-run"): child})
    result = await tools.collect(client, "task", "tenant1")
    assert result["complete"] and len(result["workflows"]) == 2 and result["scheduled_tool_requests"] == 1
    assert result["workflows"][1]["parent_execution"] == {"workflow_id": "task", "run_id": "root-run"}
    assert (child_id, "child-run") in client.requests
    child.events[0].workflow_execution_started_event_attributes.parent_workflow_execution.workflow_id = "unrelated"
    with pytest.raises(ValueError, match="parent"):
        await tools.collect(client, "task", "tenant1")


async def test_child_tenant_is_checked_before_any_payload_decode():
    child_id = "task/r1/child"
    root = WorkflowHistory("task", [await begin(),
        HistoryEvent(event_id=2, start_child_workflow_execution_initiated_event_attributes=StartChildWorkflowExecutionInitiatedEventAttributes(
            workflow_id=child_id, workflow_type=WorkflowType(name="SubAgentWorkflow"))),
        HistoryEvent(event_id=3, child_workflow_execution_started_event_attributes=ChildWorkflowExecutionStartedEventAttributes(
            initiated_event_id=2, namespace="isolated-eval", workflow_type=WorkflowType(name="SubAgentWorkflow"),
            workflow_execution=WorkflowExecution(workflow_id=child_id, run_id="child-run"))), closed(4)])
    other = WorkflowHistory(child_id, [await begin("other", parent=("task", "root-run")), closed(2)])
    with pytest.raises(ValueError, match="tenant"):
        await tools.collect(FakeClient({("task", "root-run"): root, (child_id, "child-run"): other}), "task", "tenant1")


async def model_scheduled(event_id, value, *, kind="execute_model_node", cc=None):
    payloads = await DataConverter.default.encode([value])
    if cc:
        tenant = value.tenant.tenant_id if hasattr(value, "tenant") else value["tenant"]["tenant_id"]
        payloads = await cc.bind(TenantContext(tenant_id=tenant)).encode(payloads)
    return HistoryEvent(event_id=event_id, activity_task_scheduled_event_attributes=ActivityTaskScheduledEventAttributes(
        activity_id=str(event_id), activity_type=ActivityType(name=kind), input=Payloads(payloads=payloads)))


def timed(events):
    for event in events:
        event.event_time.FromMilliseconds(100000 + int(event.event_id) * 1000)
    return events


async def test_actual_revise_judges_override_enclosing_deliver_attribution():
    rows = [
        await begin(),
        await model_scheduled(2, NodeInput(Tenant("tenant1"), "task", 1,
                              {"id": "deliver_answer.1", "role": "deliver", "inputs": {"request": "SECRET prompt"}})),
        started(3, 2), completed(4, 2, 3),
        await model_scheduled(5, NodeInput(Tenant("tenant1"), "task", 1,
                              {"id": "deliver_answer.2", "role": "judge", "inputs": {"candidate": "PRIVATE output"}})),
        started(6, 5), completed(7, 5, 6),
        await model_scheduled(8, NodeInput(Tenant("tenant1"), "task", 1, {"id": "deliver_answer.4", "role": "judge"})),
        started(9, 8), completed(10, 8, 9), closed(11),
    ]
    result = await tools.model_role_evidence(WorkflowHistory("task", timed(rows)), "tenant1", run_id="root-run")
    assert result["complete"] and not result["requires_time_resolution"]
    assert result["node_roles"] == {"deliver_answer.1": "deliver", "deliver_answer.2": "judge", "deliver_answer.4": "judge"}
    judge = result["observations"][1]
    assert judge["scheduled_event_id"] == 5 and judge["scheduled_at_unix_ms"] == 105000 and judge["terminal_at_unix_ms"] == 107000
    assert judge["started_events"] == [{"event_id": 6, "attempt": 1, "at": "1970-01-01T00:01:46Z", "at_unix_ms": 106000}]
    assert judge["context_node_id"] == "deliver_answer.2" and len(judge["input_payload_sha256"][0]) == 64
    assert "SECRET" not in json.dumps(result) and "PRIVATE" not in json.dumps(result)


async def test_subagent_toolloop_wire_act_uses_step_id_and_context_uses_goal_id():
    value = ToolLoopStepInput(Tenant("tenant1"), "task", 2, {"id": "research", "role": "deliver"}, 1,
                              [{"observation": "SECRET"}], step_id="research.1", isolated=True)
    history = WorkflowHistory("task/r2/research", timed([
        await begin(parent=("task", "root-run"), workflow_type="SubAgentWorkflow"),
        await model_scheduled(2, value, kind="tool_loop_step"), started(3, 2), completed(4, 2, 3), closed(5),
    ]))
    result = await tools.model_role_evidence(history, "tenant1", run_id="child-run")
    assert result["complete"] and result["node_roles"] == {"research.1": "act"}
    assert result["observations"][0]["context_node_id"] == "research"
    assert result["observations"][0]["task_id"] == "task"
    assert "SECRET" not in json.dumps(result)


async def test_round_conflicts_keep_both_windows_without_last_role_wins():
    history = WorkflowHistory("task", timed([
        await begin(), await model_scheduled(2, NodeInput(Tenant("tenant1"), "task", 1, {"id": "same", "role": "deliver"})),
        started(3, 2), completed(4, 2, 3),
        await model_scheduled(5, NodeInput(Tenant("tenant1"), "task", 2, {"id": "same", "role": "judge"})),
        started(6, 5), completed(7, 5, 6), closed(8),
    ]))
    result = await tools.model_role_evidence(history, "tenant1")
    assert result["complete"] and result["requires_time_resolution"]
    assert result["node_roles"] == {} and result["conflicts"][0]["roles"] == ["deliver", "judge"]
    assert [(r["round_seq"], r["scheduled_at_unix_ms"], r["terminal_at_unix_ms"]) for r in result["observations"]] == [(1, 102000, 104000), (2, 105000, 107000)]
    merged = tools.merge_model_roles([result, result], "task")
    assert merged["complete"] and merged["requires_time_resolution"] and len(merged["observations"]) == 2


async def test_model_claim_check_and_input_tenant_are_verified(tmp_path):
    cc = ClaimCheck(LocalDiskObjectStoreProvider(tmp_path / "objects", signing_key=b"test"),
                    LocalFileSecretStoreProvider(tmp_path / "secrets.json"), threshold=1)
    history = WorkflowHistory("task", timed([
        await begin(), await model_scheduled(2, NodeInput(Tenant("tenant1"), "task", 1,
                                                         {"id": "judge", "role": "judge", "inputs": {"token": "SECRET"}}), cc=cc),
        started(3, 2), completed(4, 2, 3), closed(5),
    ]))
    good = await tools.model_role_evidence(history, "tenant1", claim_check=cc)
    assert good["complete"] and good["observations"][0]["claim_check_decoded"]
    assert "SECRET" not in json.dumps(good)
    assert not (await tools.model_role_evidence(history, "tenant1"))["complete"]
    with pytest.raises(ValueError, match="tenant"):
        await tools.model_role_evidence(history, "other", claim_check=cc)
    bad_input = WorkflowHistory("task", timed([
        await begin(), await model_scheduled(2, NodeInput(Tenant("other"), "task", 1, {"id": "judge", "role": "judge"})),
        started(3, 2), completed(4, 2, 3), closed(5),
    ]))
    bad = await tools.model_role_evidence(bad_input, "tenant1")
    assert not bad["complete"] and not bad["node_roles"] and bad["errors"]


async def test_virtual_planner_and_reviewer_windows_allow_reserved_node_name_collisions():
    decision = {"tenant": {"tenant_id": "tenant1"}, "task_id": "task", "round_seq": 1, "message": "SECRET"}
    history = WorkflowHistory("task", timed([
        await begin(), await model_scheduled(2, decision, kind="plan_round"), started(3, 2), completed(4, 2, 3),
        await model_scheduled(5, NodeInput(Tenant("tenant1"), "task", 1, {"id": "plan", "role": "transform"})),
        started(6, 5), completed(7, 5, 6),
        await model_scheduled(8, decision, kind="review_round"), started(9, 8), completed(10, 8, 9), closed(11),
    ]))
    result = await tools.model_role_evidence(history, "tenant1")
    assert result["complete"] and result["requires_time_resolution"]
    assert result["node_roles"] == {"triage": "route", "review": "judge"}
    assert len(result["observations"]) == 4 and "SECRET" not in json.dumps(result)
    planner = [row for row in result["observations"] if row["activity"] == "plan_round"]
    assert {row["node_id"] for row in planner} == {"plan", "triage"}
    assert all(row["context_node_id"] is None and row["terminal_at_unix_ms"] == 104000 for row in planner)


async def test_cross_history_role_conflict_and_missing_terminal_window_remain_explicit():
    root = WorkflowHistory("task", timed([
        await begin(), await model_scheduled(2, NodeInput(Tenant("tenant1"), "task", 1, {"id": "same", "role": "deliver"})),
        started(3, 2), completed(4, 2, 3), closed(5),
    ]))
    child = WorkflowHistory("task/r2/graph", timed([
        await begin(parent=("task", "root-run"), workflow_type="SubgraphWorkflow"),
        await model_scheduled(2, NodeInput(Tenant("tenant1"), "task", 2, {"id": "same", "role": "judge"})),
        started(3, 2), completed(4, 2, 3), closed(5),
    ]))
    combined = tools.merge_model_roles([
        await tools.model_role_evidence(root, "tenant1", run_id="root-run"),
        await tools.model_role_evidence(child, "tenant1", run_id="child-run"),
    ], "task")
    assert combined["complete"] and combined["requires_time_resolution"]
    assert not combined["node_roles"] and len(combined["conflicts"][0]["sources"]) == 2
    missing = WorkflowHistory("task", [root.events[0], root.events[1], root.events[-1]])
    unresolved = await tools.model_role_evidence(missing, "tenant1")
    assert not unresolved["complete"] and unresolved["observations"][0]["terminal_at_unix_ms"] is None


async def test_explicit_no_model_decisions_do_not_invent_virtual_model_calls():
    value = {"tenant": {"tenant_id": "tenant1"}, "task_id": "task", "round_seq": 1}
    history = WorkflowHistory("task", timed([
        await begin(), await model_scheduled(2, {**value, "no_model": True}, kind="plan_round"),
        started(3, 2), completed(4, 2, 3),
        await model_scheduled(5, {**value, "rules_only": True}, kind="review_round"),
        started(6, 5), completed(7, 5, 6), closed(8),
    ]))
    result = await tools.model_role_evidence(history, "tenant1")
    assert result["complete"] and result["observations"] == [] and result["node_roles"] == {}
