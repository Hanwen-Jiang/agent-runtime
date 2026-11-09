"""Recover bounded tool results for a separate, provenance-bound evidence regrade.

Original records and the rubric stay unchanged. Only completed call_tool activities
from the selected task and tenant are accepted; no fixture contents are inferred.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from agentruntime_worker.workflows.events import redact

VERSION = 'deepeval-tool-results-v1'
MAX_TEXT = 16000
MAX_EVENTS = 200


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _bounded(value: Any) -> dict[str, Any]:
    text = redact(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return {'text': text[:MAX_TEXT], 'truncated': len(text) > MAX_TEXT}


def selected_result(tool: str, args: Mapping[str, Any], result: Mapping[str, Any], paths: frozenset[str]) -> dict[str, Any] | None:
    if tool == 'repo_read' and result.get('path') in paths:
        return {k: result[k] for k in ('path', 'content', 'start_line', 'end_line', 'truncated') if k in result}
    if tool == 'repo_grep':
        return {'matches': [r for r in result.get('matches', []) if isinstance(r, dict) and r.get('path') in paths], 'truncated': result.get('truncated', False)}
    if tool == 'shell_run':
        return {k: result[k] for k in ('exit_code', 'stdout', 'stderr', 'truncated') if k in result}
    if tool == 'repo_edit' and args.get('path') in paths:
        return {k: result[k] for k in ('path', 'changed', 'applied') if k in result}
    return None


async def extract_tools(history: Any, decode: Callable[[Any], Awaitable[list[Any]]], *, task_id: str, tenant_id: str, fixture_paths: Sequence[str]) -> dict[str, Any]:
    """Decode only matching completed activities, then retain finite public excerpts."""
    paths = frozenset(fixture_paths)
    if not task_id or not tenant_id or any(not p or p.startswith('/') or '..' in p.split('/') for p in paths):
        raise ValueError('invalid evidence scope')
    scheduled: dict[int, Any] = {}
    items = []
    for event in history.events:
        if event.HasField('activity_task_scheduled_event_attributes'):
            scheduled[event.event_id] = event.activity_task_scheduled_event_attributes
        if not event.HasField('activity_task_completed_event_attributes'):
            continue
        completed = event.activity_task_completed_event_attributes
        activity = scheduled.get(completed.scheduled_event_id)
        if activity is None or activity.activity_type.name != 'call_tool':
            continue
        inp = (await decode(activity.input.payloads))[0]
        if not isinstance(inp, dict) or inp.get('task_id') != task_id or (inp.get('tenant') or {}).get('tenant_id') != tenant_id:
            raise ValueError('tool activity is not bound to requested task and tenant')
        tool = inp.get('tool')
        if tool not in ('repo_read', 'repo_grep', 'shell_run', 'repo_edit'):
            continue
        result = (await decode(completed.result.payloads))[0]
        if not isinstance(result, Mapping):
            continue
        args = inp.get('args') or {}
        selected = selected_result(tool, args, result, paths)
        if selected is None:
            continue
        if len(items) == MAX_EVENTS:
            raise ValueError('tool evidence limit exceeded; do not silently call this complete')
        items.append({'task_id': task_id, 'round_seq': inp.get('round_seq'), 'node_id': inp.get('node_id'), 'tool': tool,
                      'activity_scheduled_event_id': completed.scheduled_event_id, 'activity_completed_event_id': event.event_id,
                      'result_payload_sha256': hashlib.sha256(completed.result.SerializeToString(deterministic=True)).hexdigest(),
                      'target': _bounded({k: args[k] for k in ('path', 'pattern', 'argv') if k in args}), 'result': _bounded(selected)})
    return {'task_id': task_id, 'tenant_id': tenant_id, 'history_sha256': hashlib.sha256(history.to_json().encode()).hexdigest(), 'events': items}


def manifest(run: Mapping[str, Any], records: Mapping[str, Any], extracted: Mapping[str, Any]) -> dict[str, Any]:
    """Tie the derived evidence to the exact original records without editing them."""
    selected = set(run['instance_ids'])
    if set(extracted) - selected:
        raise ValueError('evidence contains an unselected case')
    for case, result in extracted.items():
        if case not in records or result['task_id'] != records[case].get('task_id'):
            raise ValueError('evidence task does not match collected record')
    return {'version': VERSION, 'source': 'Temporal completed call_tool activities', 'original_run_sha256': digest(run),
            'original_record_sha256': {case: digest(records[case]) for case in sorted(extracted)},
            'subject_output_sha256': {case: digest(str(records[case].get('deliverable') or '')) for case in sorted(extracted)},
            'rubric_sha256': (run.get('identity') or {}).get('rubric_sha256'), 'evidence': dict(extracted),
            'changed_subject_outputs': False, 'changed_rubric': False, 'fresh_capability_measurement': False}


def enriched_rows(rows: Sequence[Mapping[str, Any]], evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Append actual observations to a separate scoring input; retain original notice."""
    out = []
    for row in rows:
        item = evidence['evidence'].get(row['id'])
        if item is None:
            raise ValueError('missing task-bound evidence')
        if digest(str(row.get('actual_output') or '')) != evidence['subject_output_sha256'][row['id']]:
            raise ValueError('subject output changed since evidence binding')
        extra = ['Additional recorded tool outputs from completed Temporal activities. This is evidence regrading of the same subject output, not a new task run.']
        extra.extend(json.dumps(e, ensure_ascii=False) for e in item['events'])
        out.append({**row, 'tool_evidence': [*(row.get('tool_evidence') or []), *extra]})
    return out
