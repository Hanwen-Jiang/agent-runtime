"""Read-only, tenant-bound tool request/retry evidence from Temporal histories.

Only tool names, identifiers, a canonical full-arguments digest and Temporal
attempt metadata leave the process. Arguments, outputs, failure messages and
Claim-Check URIs are never exported. Activity attempts are not proof that an
external side effect happened.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from temporalio.api.enums.v1 import RetryState, TimeoutType
from temporalio.client import Client, WorkflowHistory
from temporalio.converter import DataConverter

from agentruntime_core.adapters.claim_check import is_reference, tenant_from_history

VERSION = "temporal-tool-requests-v1"
_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:@#/-]{0,511}\Z")
_CLOSED = (
    "workflow_execution_completed_event_attributes",
    "workflow_execution_failed_event_attributes",
    "workflow_execution_canceled_event_attributes",
    "workflow_execution_terminated_event_attributes",
    "workflow_execution_timed_out_event_attributes",
    "workflow_execution_continued_as_new_event_attributes",
)
_TERMINALS = {
    "activity_task_completed_event_attributes": "completed",
    "activity_task_failed_event_attributes": "failed",
    "activity_task_timed_out_event_attributes": "timed_out",
    "activity_task_canceled_event_attributes": "canceled",
}
_WORKFLOWS = {"TaskWorkflow", "SubAgentWorkflow", "SubgraphWorkflow"}


def canonical_request_hash(tool: str, arguments: dict[str, Any]) -> str:
    """Sorted-key, compact UTF-8 JSON; preserve array order and numeric types."""
    if not isinstance(tool, str) or not _ID.fullmatch(tool) or not isinstance(arguments, dict):
        raise ValueError("invalid tool request shape")
    encoded = json.dumps({"tool": tool, "arguments": arguments}, sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _event_hash(history: WorkflowHistory) -> str:
    digest = hashlib.sha256()
    for event in history.events:
        encoded = event.SerializeToString(deterministic=True)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _enum_name(enum: Any, value: int) -> str:
    try:
        return enum.Name(value)
    except ValueError:
        return "UNRECOGNIZED"


async def history_evidence(history: WorkflowHistory, tenant_id: str, *, run_id: str = "",
                           claim_check: Any = None) -> dict[str, Any]:
    owner = tenant_from_history(history)
    if owner is None or owner.tenant_id != tenant_id:
        raise ValueError("tool history tenant does not match the experiment")
    if not _ID.fullmatch(history.workflow_id):
        raise ValueError("invalid workflow identity")
    event_ids = [int(event.event_id) for event in history.events]
    if len(set(event_ids)) != len(event_ids) or event_ids != sorted(event_ids):
        raise ValueError("history event identities are not unique and ordered")
    started, terminal = {}, {}
    for event in history.events:
        if event.HasField("activity_task_started_event_attributes"):
            attrs = event.activity_task_started_event_attributes
            started.setdefault(int(attrs.scheduled_event_id), []).append((event, attrs))
        for field, state in _TERMINALS.items():
            if event.HasField(field):
                attrs = getattr(event, field)
                key = int(attrs.scheduled_event_id)
                if key in terminal:
                    raise ValueError("conflicting terminal events for one activity")
                terminal[key] = (event, attrs, state)
    calls, errors = [], []
    for event in history.events:
        if not event.HasField("activity_task_scheduled_event_attributes"):
            continue
        attrs = event.activity_task_scheduled_event_attributes
        if attrs.activity_type.name != "call_tool":
            continue
        scheduled = int(event.event_id)
        row: dict[str, Any] = {"workflow_id": history.workflow_id, "run_id": run_id,
                              "scheduled_event_id": scheduled, "activity_id": attrs.activity_id,
                              "scheduled_at": event.event_time.ToJsonString()}
        payloads = list(attrs.input.payloads)
        try:
            referenced = any(is_reference(p) for p in payloads)
            if referenced:
                if claim_check is None:
                    raise ValueError("claim_check_reader_required")
                payloads = await claim_check.bind(owner).decode(payloads)
            decoded = await DataConverter.default.decode(payloads)
            if len(decoded) != 1 or not isinstance(decoded[0], dict):
                raise ValueError("invalid_tool_input")
            value = decoded[0]
            if not isinstance(value.get("tenant"), dict) or value["tenant"].get("tenant_id") != tenant_id:
                raise ValueError("tool_input_tenant_mismatch")
            task, node = value.get("task_id"), value.get("node_id")
            if not isinstance(task, str) or not _ID.fullmatch(task) or not isinstance(node, str) or not _ID.fullmatch(node):
                raise ValueError("invalid_tool_input_identity")
            if task != history.workflow_id.split("/r", 1)[0]:
                raise ValueError("tool_input_task_mismatch")
            row.update(task_id=task, node_id=node, tool=value["tool"],
                       request_sha256=canonical_request_hash(value["tool"], value["args"]),
                       claim_check_decoded=referenced)
        except Exception as exc:
            # Errors may contain raw arguments or a signed object reference.
            errors.append({"scheduled_event_id": scheduled, "error_type": type(exc).__name__})
            row["input_state"] = "unresolved"
        else:
            row["input_state"] = "decoded"
        starts = started.get(scheduled, [])
        attempts = [int(a.attempt) for _, a in starts]
        row["observed_started_events"] = [
            {"event_id": int(e.event_id), "attempt": int(a.attempt),
             "started_at": e.event_time.ToJsonString(), "previous_failure_present": a.HasField("last_failure")}
            for e, a in starts
        ]
        maximum = max(attempts, default=0)
        row["reported_started_attempt"] = maximum or None
        row["reported_retries_before_latest_start"] = max(0, maximum - 1) if maximum else None
        end = terminal.get(scheduled)
        valid_starts = not any(a < 1 for a in attempts)
        if end:
            e, a, state = end
            row.update(state=state, terminal_event_id=int(e.event_id),
                       terminal_at=e.event_time.ToJsonString(), terminal_started_event_id=int(a.started_event_id))
            if hasattr(a, "retry_state"):
                row["retry_state"] = _enum_name(RetryState, a.retry_state)
            if state == "timed_out":
                row["timeout_type"] = _enum_name(TimeoutType, a.failure.timeout_failure_info.timeout_type)
            # A scheduled-only open history can hide a buffered started event.
            # A terminal event explicitly without a start is distinguishable.
            no_start_proven = state == "timed_out" and a.failure.timeout_failure_info.timeout_type == TimeoutType.TIMEOUT_TYPE_SCHEDULE_TO_START
            no_start_proven |= state == "canceled" and attrs.retry_policy.maximum_attempts == 1
            if not a.started_event_id and not starts and no_start_proven:
                row["start_state"] = "not_started"
                row["reported_started_attempt"] = 0
                row["reported_retries_before_latest_start"] = 0
            elif a.started_event_id in {int(e.event_id) for e, _ in starts} and valid_starts:
                row["start_state"] = "started"
            else:
                row["start_state"] = "unresolved"
                errors.append({"scheduled_event_id": scheduled, "error_type": "MissingStartedEvent"})
        else:
            row["state"] = "unresolved"
            row["start_state"] = "started" if starts and valid_starts else "not_observed"
        if not valid_starts:
            errors.append({"scheduled_event_id": scheduled, "error_type": "InvalidAttempt"})
        calls.append(row)
    closed = any(event.HasField(field) for event in history.events for field in _CLOSED)
    continued = any(event.HasField("workflow_execution_continued_as_new_event_attributes") for event in history.events)
    complete = closed and not continued and not errors and all(r["state"] != "unresolved" and r["start_state"] != "unresolved" for r in calls)
    return {"workflow_id": history.workflow_id, "run_id": run_id, "tenant_id": tenant_id,
            "history_sha256": _event_hash(history), "workflow_closed": closed, "continued_as_new": continued,
            "complete": complete, "calls": calls, "errors": errors}


def summarize(workflows: list[dict[str, Any]], *, tree_errors: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = [row for workflow in workflows for row in workflow["calls"]]
    keyed = [row for row in rows if row.get("input_state") == "decoded"]
    counts = Counter(row["request_sha256"] for row in keyed)
    started_counts = Counter(row["request_sha256"] for row in keyed if (row["reported_started_attempt"] or 0) > 0)
    groups = [{"request_sha256": key, "scheduled_requests": count,
               "identities": [{"workflow_id": row["workflow_id"], "run_id": row["run_id"],
                               "scheduled_event_id": row["scheduled_event_id"]} for row in keyed if row["request_sha256"] == key]}
              for key, count in sorted(counts.items()) if count > 1]
    known_attempts = [r["reported_started_attempt"] for r in rows if r["reported_started_attempt"] is not None]
    return {"schema_version": VERSION, "complete": bool(workflows) and all(w["complete"] for w in workflows) and not tree_errors,
            "scheduled_tool_requests": len(rows), "decoded_tool_requests": len(keyed),
            "repeated_full_requests": sum(n - 1 for n in counts.values()),
            "repeated_full_started_requests": sum(n - 1 for n in started_counts.values()),
            "repeated_request_groups": groups,
            "reported_started_attempts_lower_bound": sum(known_attempts),
            "reported_retry_attempts_lower_bound": sum(max(0, n - 1) for n in known_attempts),
            "unresolved_activity_count": sum(r["state"] == "unresolved" or r["start_state"] in ("unresolved", "not_observed") for r in rows),
            "not_started_activity_count": sum(r["start_state"] == "not_started" for r in rows),
            "workflows": workflows, "tree_errors": tree_errors or [],
            "hash_contract": "SHA256(sorted-key compact UTF-8 JSON of tool and complete args; arrays and numeric types preserved)",
            "limitations": [
                "Repeated scheduled full requests are separate decisions; Temporal retries remain within one scheduled request.",
                "Started attempt counters report activity attempts, not external side-effect counts or successful tool executions.",
                "Temporal may omit intermediate retry events; only reported counters and observed terminal/failure metadata are exported.",
                "Open, missing-start, continued-as-new and undecodable histories remain incomplete; no zero-filled success is inferred.",
            ]}


async def collect(client: Any, workflow_id: str, tenant_id: str, *, claim_check: Any = None,
                  run_id: str | None = None, max_workflows: int = 128) -> dict[str, Any]:
    if not _ID.fullmatch(workflow_id) or max_workflows < 1:
        raise ValueError("invalid root workflow identity or tree bound")
    queue = [(workflow_id, run_id, None)]
    seen, workflows, errors = set(), [], []
    while queue:
        wid, requested_run, parent = queue.pop(0)
        handle = client.get_workflow_handle(wid, run_id=requested_run)
        if requested_run is None:
            requested_run = (await handle.describe()).run_id
            handle = client.get_workflow_handle(wid, run_id=requested_run)
        identity = (wid, requested_run)
        if identity in seen:
            continue
        if len(seen) >= max_workflows:
            errors.append({"workflow_id": wid, "error_type": "WorkflowTreeLimit"})
            break
        seen.add(identity)
        history = await handle.fetch_history()
        owner = tenant_from_history(history)
        if owner is None or owner.tenant_id != tenant_id:
            raise ValueError("child or root history belongs to another tenant")
        first = history.events[0].workflow_execution_started_event_attributes
        if parent is not None and (first.parent_workflow_execution.workflow_id, first.parent_workflow_execution.run_id) != parent:
            raise ValueError("child history does not match its recorded parent execution")
        result = await history_evidence(history, tenant_id, run_id=requested_run or "", claim_check=claim_check)
        result["parent_execution"] = None if parent is None else {"workflow_id": parent[0], "run_id": parent[1]}
        workflows.append(result)
        initiated = {int(e.event_id): e.start_child_workflow_execution_initiated_event_attributes for e in history.events
                     if e.HasField("start_child_workflow_execution_initiated_event_attributes")}
        child_starts, child_failures = set(), set()
        for event in history.events:
            if event.HasField("start_child_workflow_execution_failed_event_attributes"):
                child_failures.add(int(event.start_child_workflow_execution_failed_event_attributes.initiated_event_id))
            if not event.HasField("child_workflow_execution_started_event_attributes"):
                continue
            child = event.child_workflow_execution_started_event_attributes
            init = initiated.get(int(child.initiated_event_id))
            if init is None or init.workflow_id != child.workflow_execution.workflow_id or init.workflow_type.name != child.workflow_type.name:
                raise ValueError("child start does not match its initiation")
            child_starts.add(int(child.initiated_event_id))
            if child.namespace and child.namespace != client.namespace:
                raise ValueError("cross-namespace child evidence is not authorized")
            if child.workflow_type.name not in _WORKFLOWS or not _ID.fullmatch(child.workflow_execution.workflow_id) or not child.workflow_execution.run_id:
                errors.append({"workflow_id": wid, "event_id": int(event.event_id), "error_type": "UnsupportedChildExecution"})
                continue
            queue.append((child.workflow_execution.workflow_id, child.workflow_execution.run_id, identity))
        unresolved = sorted(set(initiated) - child_starts - child_failures)
        if unresolved:
            errors.append({"workflow_id": wid, "error_type": "UnresolvedChildStart", "initiated_event_ids": unresolved})
    result = summarize(workflows, tree_errors=errors)
    result.update(root_workflow_id=workflow_id, tenant_id=tenant_id, namespace=client.namespace)
    return result


async def _live(args: argparse.Namespace) -> dict[str, Any]:
    from agentruntime_worker.replay import claim_check_from_env
    address = os.environ.get("AR_TEMPORAL_ADDRESS", "")
    namespace = os.environ.get("AR_TEMPORAL_NAMESPACE", "")
    if not address or not namespace:
        raise ValueError("explicit Temporal address and namespace are required")
    client = await Client.connect(address, namespace=namespace)
    return await collect(client, args.task_id, args.tenant, run_id=args.run_id, claim_check=claim_check_from_env())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--out", required=True, help="destination JSON, or - for stdout inside an existing worker")
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(_live(args))
    except Exception as exc:
        # Neither signed object references nor provider exception payloads leave.
        print(json.dumps({"complete": False, "error_type": type(exc).__name__}), file=sys.stderr)
        return 4
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out == "-":
        print(text, end="")
    else:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(text)
        print(json.dumps({"complete": result["complete"], "scheduled_tool_requests": result["scheduled_tool_requests"], "out": str(path)}))
    return 0 if result["complete"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
