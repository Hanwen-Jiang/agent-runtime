"""在 local activity 的 marker 之后插入一次工作流任务切分（S-04 缺陷修复的回归测试用，见 workflows/patches.py）。

真实的切分：local activity 运行时来了查询（例如客户端轮询 QueryTask），SDK 只带着这个 marker 先提交工作流任务，
紧接着开始下一个工作流任务，local activity 之后的命令记在下一个工作流任务里：

    WFT_COMPLETED  MARKER(local activity)  WFT_SCHEDULED  WFT_STARTED  WFT_COMPLETED  <之后的命令>…

这里在一份正常的历史上造出同样的形状：marker 之后插入 WFT_SCHEDULED、WFT_STARTED、WFT_COMPLETED 三个事件，
其后的事件 id 顺延，引用事件 id 的字段一并改，之后的命令改记在新的 WFT_COMPLETED 下。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import WorkflowHistory

LOCAL_ACTIVITY_MARKER = "core_local_activity"
PATCH_MARKER = "core_patch"
# 工作流任务完成后由命令产生的事件：marker 之后紧跟着这些，说明同一个工作流任务里还有命令。
COMMAND_EVENTS = frozenset(
    {
        EventType.EVENT_TYPE_MARKER_RECORDED,
        EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED,
        EventType.EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED,
        EventType.EVENT_TYPE_TIMER_STARTED,
        EventType.EVENT_TYPE_TIMER_CANCELED,
        EventType.EVENT_TYPE_UPSERT_WORKFLOW_SEARCH_ATTRIBUTES,
        EventType.EVENT_TYPE_WORKFLOW_PROPERTIES_MODIFIED,
        EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED,
        EventType.EVENT_TYPE_SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED,
        EventType.EVENT_TYPE_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED,
        EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
        EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED,
        EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED,
        EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW,
    }
)
WFT_SCHEDULED = EventType.EVENT_TYPE_WORKFLOW_TASK_SCHEDULED
WFT_STARTED = EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED
WFT_COMPLETED = EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED


def is_local_activity_marker(event: HistoryEvent) -> bool:
    return event.event_type == EventType.EVENT_TYPE_MARKER_RECORDED and event.marker_recorded_event_attributes.marker_name == LOCAL_ACTIVITY_MARKER


def patch_id(event: HistoryEvent) -> str | None:
    """版本分支标记里的 patch id；不是版本分支标记时为 None。"""
    if event.event_type != EventType.EVENT_TYPE_MARKER_RECORDED or event.marker_recorded_event_attributes.marker_name != PATCH_MARKER:
        return None
    payload = event.marker_recorded_event_attributes.details["patch-data"].payloads[0]
    return str(json.loads(payload.data)["id"])


def first_task_patches(events: Sequence[HistoryEvent]) -> tuple[list[str], list[str]]:
    """(第一个工作流任务里问的版本分支, 之后才问的版本分支)，各按出现的先后。"""
    first = next(i for i, e in enumerate(events) if e.event_type == WFT_COMPLETED)
    end = next((i for i in range(first + 1, len(events)) if events[i].event_type not in COMMAND_EVENTS), len(events))
    ids = [(i, patch_id(e)) for i, e in enumerate(events)]
    return [p for i, p in ids if p and first < i < end], [p for i, p in ids if p and i >= end]


def split_points(events: Sequence[HistoryEvent], *, before_activity: str | None = None) -> list[int]:
    """可以切开的位置（marker 的下标）：local activity 的 marker 之后，同一个工作流任务里还有命令。
    before_activity 给出时，只要紧挨在这个 activity 的调度之前（中间只隔着别的 marker、upsert 等命令）的那几处。"""
    points = [i for i in range(len(events) - 1) if is_local_activity_marker(events[i]) and events[i + 1].event_type in COMMAND_EVENTS]
    if before_activity is None:
        return points
    found: set[int] = set()
    for j, event in enumerate(events):
        if event.event_type != EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            continue
        if event.activity_task_scheduled_event_attributes.activity_type.name != before_activity:
            continue
        i = j - 1
        while i >= 0 and events[i].event_type in COMMAND_EVENTS and not is_local_activity_marker(events[i]):
            i -= 1
        if i in points:
            found.add(i)
    return sorted(found)


def window(events: Sequence[HistoryEvent], start: int) -> list[str]:
    """从 start 起到下一个 activity 调度为止的事件类型，用来比较切开处的形状。"""
    out = []
    for event in events[start:]:
        out.append(EventType.Name(event.event_type).removeprefix("EVENT_TYPE_"))
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            break
    return out


def _attributes(event: HistoryEvent):  # type: ignore[no-untyped-def]
    name = event.WhichOneof("attributes")
    return getattr(event, name) if name else None


def _remap(event: HistoryEvent, ids: dict[int, int]) -> None:
    attrs = _attributes(event)
    if attrs is None:
        return
    for field, value in attrs.ListFields():
        if field.name.endswith("_event_id") and isinstance(value, int) and value in ids:
            setattr(attrs, field.name, ids[value])


def _copy(event: HistoryEvent, event_id: int, at: HistoryEvent) -> HistoryEvent:
    out = HistoryEvent()
    out.CopyFrom(event)
    out.event_id = event_id
    out.event_time.CopyFrom(at.event_time)
    out.task_id = at.task_id
    return out


def split_after(history: WorkflowHistory, points: Sequence[int]) -> WorkflowHistory:
    """在 points（marker 的下标）每一处之后插入一次工作流任务切分。"""
    events = list(history.events)
    at = sorted(set(points))
    for i in at:
        assert is_local_activity_marker(events[i]) and events[i + 1].event_type in COMMAND_EVENTS, f"第 {i} 个事件之后不能切"
    ids: dict[int, int] = {}
    shift = 0
    for i, event in enumerate(events):
        ids[event.event_id] = event.event_id + shift
        if i in at:
            shift += 3
    out: list[HistoryEvent] = []
    last: dict[int, HistoryEvent] = {}
    completed_by: int | None = None
    for i, original in enumerate(events):
        event = HistoryEvent()
        event.CopyFrom(original)
        event.event_id = ids[original.event_id]
        _remap(event, ids)
        if event.event_type in (WFT_SCHEDULED, WFT_STARTED, WFT_COMPLETED):
            last[event.event_type] = event
            if event.event_type == WFT_SCHEDULED:
                completed_by = None
        elif completed_by is not None:
            attrs = _attributes(event)
            if attrs is not None and "workflow_task_completed_event_id" in attrs.DESCRIPTOR.fields_by_name:
                attrs.workflow_task_completed_event_id = completed_by
        out.append(event)
        if i not in at:
            continue
        base = event.event_id
        scheduled = _copy(last[WFT_SCHEDULED], base + 1, event)
        scheduled.workflow_task_scheduled_event_attributes.attempt = 1
        started = _copy(last[WFT_STARTED], base + 2, event)
        started.workflow_task_started_event_attributes.scheduled_event_id = base + 1
        completed = _copy(last[WFT_COMPLETED], base + 3, event)
        completed.workflow_task_completed_event_attributes.scheduled_event_id = base + 1
        completed.workflow_task_completed_event_attributes.started_event_id = base + 2
        # SDK 的 flag 只在第一次用到时记下；切出来的工作流任务不带。
        completed.workflow_task_completed_event_attributes.ClearField("sdk_metadata")
        out.extend([scheduled, started, completed])
        completed_by = base + 3
    return WorkflowHistory(history.workflow_id, out)
