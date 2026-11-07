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
_MODEL_ROLES = {"fetch", "transform", "plan", "route", "judge", "aggregate", "act", "gate", "deliver"}


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


def _role_map(observations: list[dict[str, Any]]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in observations:
        grouped.setdefault(row["node_id"], []).append(row)
    roles, conflicts = {}, []
    for node_id, rows in sorted(grouped.items()):
        values = sorted({row["role"] for row in rows})
        if len(values) == 1:
            roles[node_id] = values[0]
        else:
            conflicts.append({"node_id": node_id, "roles": values,
                              "sources": [{"workflow_id": row["workflow_id"], "run_id": row["run_id"],
                                           "round_seq": row["round_seq"], "scheduled_event_id": row["scheduled_event_id"]}
                                          for row in rows]})
    return roles, conflicts


def _activity_windows(history: WorkflowHistory) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            result[int(event.event_id)] = {
                "scheduled_at": event.event_time.ToJsonString() if event.HasField("event_time") else None,
                "scheduled_at_unix_ms": event.event_time.ToMilliseconds() if event.HasField("event_time") else None,
                "started_events": [], "terminal_event_id": None, "terminal_at": None,
                "terminal_at_unix_ms": None, "terminal_state": None,
            }
        elif event.HasField("activity_task_started_event_attributes"):
            attrs = event.activity_task_started_event_attributes
            row = result.get(int(attrs.scheduled_event_id))
            if row is not None:
                row["started_events"].append({
                    "event_id": int(event.event_id), "attempt": int(attrs.attempt),
                    "at": event.event_time.ToJsonString() if event.HasField("event_time") else None,
                    "at_unix_ms": event.event_time.ToMilliseconds() if event.HasField("event_time") else None,
                })
        else:
            for field, state in _TERMINALS.items():
                if not event.HasField(field):
                    continue
                attrs = getattr(event, field)
                row = result.get(int(attrs.scheduled_event_id))
                if row is not None:
                    if row["terminal_event_id"] is not None:
                        raise ValueError("conflicting model activity terminal events")
                    row.update(
                        terminal_event_id=int(event.event_id), terminal_state=state,
                        terminal_started_event_id=int(attrs.started_event_id),
                        terminal_at=event.event_time.ToJsonString() if event.HasField("event_time") else None,
                        terminal_at_unix_ms=event.event_time.ToMilliseconds() if event.HasField("event_time") else None,
                    )
    for row in result.values():
        start, end = row["scheduled_at_unix_ms"], row["terminal_at_unix_ms"]
        row["window_start_unix_ms"] = start
        row["window_end_unix_ms"] = end
        row["window_complete"] = start is not None and end is not None and start <= end
        row["window_basis"] = "scheduled-to-terminal bounds include earlier retries whose starts Temporal may omit"
    return result


async def model_role_evidence(history: WorkflowHistory, tenant_id: str, *, run_id: str = "",
                              claim_check: Any = None) -> dict[str, Any]:
    """Attribute wire roles from actual expanded model activity inputs.

    A revise wrapper's child judge is a judge even when its enclosing node is
    deliver. A tool-loop thought always uses ACT, including sub-agent thoughts.
    Flat node IDs reused with distinct roles remain ambiguous because the usage
    ledger has no Round column; no last-write-wins role inference is permitted.
    """
    owner = tenant_from_history(history)
    if owner is None or owner.tenant_id != tenant_id:
        raise ValueError("model activity history tenant does not match the experiment")
    if not _ID.fullmatch(history.workflow_id):
        raise ValueError("invalid workflow identity")
    event_ids = [int(event.event_id) for event in history.events]
    if event_ids != sorted(set(event_ids)):
        raise ValueError("model history event identities are not unique and ordered")
    observations, errors = [], []
    windows = _activity_windows(history)
    seen = set()
    expected_task = history.workflow_id.split("/r", 1)[0]
    for event in history.events:
        if int(event.event_id) in seen:
            raise ValueError("duplicate history event identity")
        seen.add(int(event.event_id))
        if not event.HasField("activity_task_scheduled_event_attributes"):
            continue
        scheduled = event.activity_task_scheduled_event_attributes
        kind = scheduled.activity_type.name
        if kind not in ("execute_model_node", "tool_loop_step", "plan_round", "review_round"):
            continue
        payloads = list(scheduled.input.payloads)
        wire_digests = [hashlib.sha256(p.SerializeToString(deterministic=True)).hexdigest() for p in payloads]
        referenced = any(is_reference(p) for p in payloads)
        try:
            if referenced:
                if claim_check is None:
                    raise ValueError("claim_check_reader_required")
                payloads = await claim_check.bind(owner).decode(payloads)
            decoded = await DataConverter.default.decode(payloads)
            if len(decoded) != 1 or not isinstance(decoded[0], dict):
                raise ValueError("invalid_model_activity_input")
            value = decoded[0]
            if not isinstance(value.get("tenant"), dict) or value["tenant"].get("tenant_id") != tenant_id:
                raise ValueError("model_input_tenant_mismatch")
            if value.get("task_id") != expected_task:
                raise ValueError("model_input_task_mismatch")
            round_seq = value.get("round_seq")
            if not isinstance(round_seq, int) or isinstance(round_seq, bool) or round_seq < 1:
                raise ValueError("invalid_model_round")
            if kind == "plan_round":
                entries = [] if value.get("no_model") is True else [("plan", "plan"), ("triage", "route")]
                context_node_id = None
            elif kind == "review_round":
                entries = [] if value.get("rules_only") is True else [("review", "judge")]
                context_node_id = None
            else:
                node = value.get("node")
                if not isinstance(node, dict):
                    raise ValueError("missing_model_node")
                context_node_id = node.get("id")
                if not isinstance(context_node_id, str) or not _ID.fullmatch(context_node_id):
                    raise ValueError("invalid_context_node_identity")
                entries = [(node.get("id"), node.get("role"))] if kind == "execute_model_node" else [(value.get("step_id") or node.get("id"), "act")]
            for node_id, role in entries:
                if not isinstance(node_id, str) or not _ID.fullmatch(node_id) or role not in _MODEL_ROLES:
                    raise ValueError("invalid_model_node_identity_or_role")
                observations.append({"workflow_id": history.workflow_id, "run_id": run_id,
                                     "task_id": expected_task, "round_seq": round_seq,
                                     "scheduled_event_id": int(event.event_id), "activity": kind,
                                     "node_id": node_id, "context_node_id": context_node_id,
                                     "role": role, "claim_check_decoded": referenced,
                                     "input_payload_sha256": wire_digests,
                                     "decoded_payload_sha256": [hashlib.sha256(p.SerializeToString(deterministic=True)).hexdigest() for p in payloads],
                                     **windows[int(event.event_id)]})
        except Exception as exc:
            errors.append({"workflow_id": history.workflow_id, "scheduled_event_id": int(event.event_id),
                           "activity": kind, "input_payload_sha256": wire_digests,
                           "claim_check": referenced, "error_type": type(exc).__name__})
    node_roles, conflicts = _role_map(observations)
    closed = any(event.HasField(field) for event in history.events for field in _CLOSED)
    continued = any(event.HasField("workflow_execution_continued_as_new_event_attributes") for event in history.events)
    return {"schema_version": "temporal-model-roles-v1", "tenant_id": tenant_id,
            "workflow_id": history.workflow_id, "run_id": run_id, "task_id": expected_task,
            "history_sha256": _event_hash(history), "workflow_closed": closed,
            "complete": closed and not continued and not errors and all(row["window_complete"] for row in observations),
            "requires_time_resolution": bool(conflicts),
            "node_roles": node_roles, "observations": observations, "conflicts": conflicts, "errors": errors,
            "scope": "wire role attribution from actual scheduled activity inputs; not a new model execution or scoring rule"}


def merge_model_roles(evidences: list[dict[str, Any]], task_id: str) -> dict[str, Any]:
    """Merge root/subgraph/sub-agent evidence, checking cross-history conflicts."""
    if not evidences or not _ID.fullmatch(task_id) or len({e["tenant_id"] for e in evidences}) != 1:
        raise ValueError("model-role evidence must have one tenant and a valid Task")
    selected = [e for e in evidences if e["task_id"] == task_id]
    observations, seen = [], {}
    for evidence in selected:
        for row in evidence["observations"]:
            identity = (row["workflow_id"], row["run_id"], row["scheduled_event_id"], row["node_id"])
            if identity in seen and seen[identity] != row:
                raise ValueError("conflicting evidence for the same activity")
            if identity not in seen:
                observations.append(row)
                seen[identity] = row
    roles, conflicts = _role_map(observations)
    errors = [error for e in selected for error in e["errors"]]
    return {"schema_version": "temporal-model-roles-v1", "tenant_id": evidences[0]["tenant_id"], "task_id": task_id,
            "complete": bool(selected) and all(e["complete"] for e in selected) and not errors,
            "requires_time_resolution": bool(conflicts),
            "node_roles": roles, "observations": observations, "conflicts": conflicts, "errors": errors,
            "source_histories": [{"workflow_id": e["workflow_id"], "run_id": e["run_id"], "sha256": e["history_sha256"]} for e in selected]}


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
