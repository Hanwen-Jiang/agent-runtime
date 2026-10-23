"""事件历史的检查（计划 S-07 验收“事件历史无异常”）。

一份历史“无异常”指：

1. 没有 WorkflowTaskFailed：工作流代码没有抛异常、没有不确定性错误。唯一的例外是 UNHANDLED_COMMAND：工作流要结束
   （完成或 Continue-As-New）时恰好来了新事件（例如信号），服务端拒绝这次结束命令，让工作流先处理新事件再重试。
   只有之后有工作流任务成功完成、且这次运行正常结束（completed 或 continued_as_new）时才放行。
2. 只有 Worker 被杀过时才允许 WorkflowTaskTimedOut（被杀的 Worker 手里的工作流任务超时后转给别的 Worker）。
3. 工作流以预期的方式结束（或预期仍在运行，例如 Session 工作流）。
4. 每个调度过的 activity 都有结局；失败或超时的只能是用例预期的那些。
5. 已完成的步骤不重做：带 Node 的 activity 每个 (activity 名, Node id) 只调度一次，决策 activity 每个 Round 只调度一次；
   重试发生在同一次调度之内（attempt 增加），不会再调度一次。
6. 事件数有上限，没有失控的循环。
7. 用当前代码能回放：Task 工作流及其 Python 子工作流用 Python 的 Replayer，Session 工作流用 `task-service replay`。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from temporalio.api.enums.v1 import EventType, WorkflowTaskFailedCause
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHistory

MAX_EVENTS = 2000
UNHANDLED_COMMAND = WorkflowTaskFailedCause.WORKFLOW_TASK_FAILED_CAUSE_UNHANDLED_COMMAND
NORMAL_CLOSES = ("completed", "continued_as_new")
DECISION_ACTIVITIES = ("plan_round", "review_round", "finalize_task")
CLOSE_EVENTS = {
    "WORKFLOW_EXECUTION_COMPLETED": "completed",
    "WORKFLOW_EXECUTION_FAILED": "failed",
    "WORKFLOW_EXECUTION_CANCELED": "canceled",
    "WORKFLOW_EXECUTION_TERMINATED": "terminated",
    "WORKFLOW_EXECUTION_TIMED_OUT": "timed_out",
    "WORKFLOW_EXECUTION_CONTINUED_AS_NEW": "continued_as_new",
}
PYTHON_WORKFLOWS = ("TaskWorkflow", "SubAgentWorkflow", "SubgraphWorkflow")


def _name(event: Any) -> str:
    return EventType.Name(event.event_type).removeprefix("EVENT_TYPE_")


def _payload_json(payloads: Any) -> Any:
    """第一个载荷按 JSON 解出来（json/plain 与 json/protobuf 都是 JSON 文本）；解不出返回 None。"""
    items = list(getattr(payloads, "payloads", []) or [])
    if not items:
        return None
    try:
        return json.loads(items[0].data)
    except (ValueError, UnicodeDecodeError):
        return None


@dataclass
class ActivityRecord:
    scheduled_event_id: int
    activity_type: str
    node_id: str | None
    round_seq: int | None
    attempts: int = 0
    outcome: str = "open"
    failure: str = ""
    result: Any = None


@dataclass
class WorkflowTaskFailure:
    cause: int
    message: str
    retried: bool = False  # 之后有工作流任务成功完成

    def __str__(self) -> str:
        return f"{self.cause}: {self.message}"


@dataclass
class HistoryReport:
    workflow_id: str
    run_id: str
    workflow_type: str
    events: list[str]
    close: str | None
    activities: list[ActivityRecord] = field(default_factory=list)
    workflow_task_failed: list[WorkflowTaskFailure] = field(default_factory=list)
    workflow_task_timeouts: int = 0
    signals: list[str] = field(default_factory=list)
    timers_fired: int = 0
    max_payload_bytes: int = 0

    @property
    def workflow_task_failures(self) -> list[str]:
        """每一次 WorkflowTaskFailed，含放行的 UNHANDLED_COMMAND。"""
        return [str(f) for f in self.workflow_task_failed]

    def unexpected_workflow_task_failures(self) -> list[str]:
        """按模块文档第 1 条算作异常的 WorkflowTaskFailed。"""
        benign = self.close in NORMAL_CLOSES
        return [str(f) for f in self.workflow_task_failed if not (benign and f.cause == UNHANDLED_COMMAND and f.retried)]

    def scheduled(self, activity_type: str, node_id: str | None = None) -> list[ActivityRecord]:
        return [a for a in self.activities if a.activity_type == activity_type and (node_id is None or a.node_id == node_id)]

    def activity(self, activity_type: str, node_id: str | None = None) -> ActivityRecord:
        found = self.scheduled(activity_type, node_id)
        assert len(found) == 1, f"{self.workflow_id} 里 {activity_type}（{node_id}）应恰好调度一次，实际 {len(found)} 次"
        return found[0]


def analyze(history: WorkflowHistory) -> HistoryReport:
    events = list(history.events)
    names = [_name(e) for e in events]
    start = events[0].workflow_execution_started_event_attributes if events else None
    report = HistoryReport(
        workflow_id=history.workflow_id,
        run_id=start.original_execution_run_id if start else "",
        workflow_type=start.workflow_type.name if start else "",
        events=names,
        close=next((CLOSE_EVENTS[n] for n in reversed(names) if n in CLOSE_EVENTS), None),
    )
    by_scheduled: dict[int, ActivityRecord] = {}
    for event, name in zip(events, names, strict=True):
        for payloads in _payload_fields(event):
            for p in payloads.payloads:
                report.max_payload_bytes = max(report.max_payload_bytes, len(p.data))
        if name == "ACTIVITY_TASK_SCHEDULED":
            attrs = event.activity_task_scheduled_event_attributes
            inp = _payload_json(attrs.input) or {}
            node = inp.get("node") if isinstance(inp, dict) else None
            record = ActivityRecord(
                scheduled_event_id=event.event_id,
                activity_type=attrs.activity_type.name,
                node_id=node.get("id") if isinstance(node, dict) else None,
                round_seq=inp.get("round_seq") if isinstance(inp, dict) else None,
            )
            by_scheduled[event.event_id] = record
            report.activities.append(record)
        elif name == "ACTIVITY_TASK_STARTED":
            attrs = event.activity_task_started_event_attributes
            by_scheduled[attrs.scheduled_event_id].attempts = attrs.attempt
        elif name == "ACTIVITY_TASK_COMPLETED":
            attrs = event.activity_task_completed_event_attributes
            record = by_scheduled[attrs.scheduled_event_id]
            record.outcome = "completed"
            record.result = _payload_json(attrs.result)
        elif name in ("ACTIVITY_TASK_FAILED", "ACTIVITY_TASK_TIMED_OUT", "ACTIVITY_TASK_CANCELED"):
            attrs = {
                "ACTIVITY_TASK_FAILED": event.activity_task_failed_event_attributes,
                "ACTIVITY_TASK_TIMED_OUT": event.activity_task_timed_out_event_attributes,
                "ACTIVITY_TASK_CANCELED": event.activity_task_canceled_event_attributes,
            }[name]
            record = by_scheduled[attrs.scheduled_event_id]
            record.outcome = {"ACTIVITY_TASK_FAILED": "failed", "ACTIVITY_TASK_TIMED_OUT": "timed_out", "ACTIVITY_TASK_CANCELED": "canceled"}[name]
            failure = getattr(attrs, "failure", None)
            record.failure = failure.message if failure is not None else ""
        elif name == "WORKFLOW_TASK_FAILED":
            attrs = event.workflow_task_failed_event_attributes
            report.workflow_task_failed.append(WorkflowTaskFailure(cause=attrs.cause, message=attrs.failure.message))
        elif name == "WORKFLOW_TASK_COMPLETED":
            for failure in report.workflow_task_failed:
                failure.retried = True
        elif name == "WORKFLOW_TASK_TIMED_OUT":
            report.workflow_task_timeouts += 1
        elif name == "WORKFLOW_EXECUTION_SIGNALED":
            report.signals.append(event.workflow_execution_signaled_event_attributes.signal_name)
        elif name == "TIMER_FIRED":
            report.timers_fired += 1
    return report


def _payload_fields(event: Any) -> Iterable[Any]:
    """事件里所有 Payloads 字段（输入、结果、信号载荷等），用来量最大的载荷。"""
    attrs_name = event.WhichOneof("attributes")
    if attrs_name is None:
        return []
    attrs = getattr(event, attrs_name)
    out = []
    for descriptor, value in attrs.ListFields():
        if descriptor.message_type is not None and descriptor.message_type.full_name == "temporal.api.common.v1.Payloads":
            out.append(value)
    return out


def assert_clean(
    report: HistoryReport,
    *,
    close: str | None = "completed",
    worker_restarted: bool = False,
    allowed_failures: Sequence[tuple[str, str | None]] = (),
    allowed_rescheduled: Sequence[tuple[str, str | None]] = (),
) -> None:
    """按模块文档的 1 至 6 条检查一份历史。close 为 None 表示工作流应仍在运行。"""
    where = f"{report.workflow_type} {report.workflow_id}"
    failed = report.unexpected_workflow_task_failures()
    assert not failed, f"{where} 有 WorkflowTaskFailed：{failed}"
    if not worker_restarted:
        assert report.workflow_task_timeouts == 0, f"{where} 没杀过 Worker，却有 {report.workflow_task_timeouts} 次 WorkflowTaskTimedOut"
    assert report.close == close, f"{where} 应以 {close} 结束，实际 {report.close}"
    assert len(report.events) <= MAX_EVENTS, f"{where} 有 {len(report.events)} 个事件，超过 {MAX_EVENTS}"
    if close is not None:
        still_open = [(a.activity_type, a.node_id) for a in report.activities if a.outcome == "open"]
        assert not still_open, f"{where} 结束时还有没结局的 activity：{still_open}"
    unexpected = [
        (a.activity_type, a.node_id, a.outcome, a.failure)
        for a in report.activities
        if a.outcome in ("failed", "timed_out") and (a.activity_type, a.node_id) not in allowed_failures
    ]
    assert not unexpected, f"{where} 有预期之外的 activity 失败：{unexpected}"
    steps: Counter[tuple[str, str]] = Counter()
    for a in report.activities:
        if a.node_id is not None and (a.activity_type, a.node_id) not in allowed_rescheduled:
            steps[(a.activity_type, a.node_id)] += 1
        elif a.activity_type in DECISION_ACTIVITIES:
            steps[(a.activity_type, f"round {a.round_seq}")] += 1
    redone = [k for k, n in steps.items() if n > 1]
    assert not redone, f"{where} 已完成的步骤被重新调度：{redone}"


def assert_session_clean(runs: Sequence[WorkflowHistory], *, worker_restarted: bool = False) -> None:
    """Session 工作流的各次运行：前面的都以 Continue-As-New 结束，最后一次仍在运行。"""
    for i, run in enumerate(runs):
        assert_clean(analyze(run), close="continued_as_new" if i < len(runs) - 1 else None, worker_restarted=worker_restarted)


# --- 取历史与回放 ---


async def _fetch(address: str, workflow_id: str) -> list[WorkflowHistory]:
    """一个工作流 ID 的所有运行（Continue-As-New 之后有多次），按开始时间排序。"""
    client = await Client.connect(address)
    runs = [wf async for wf in client.list_workflows(f'WorkflowId = "{workflow_id}"')]
    runs.sort(key=lambda wf: wf.start_time)
    return [await client.get_workflow_handle(wf.id, run_id=wf.run_id).fetch_history() for wf in runs]


def fetch(address: str, workflow_id: str) -> list[WorkflowHistory]:
    return asyncio.run(_fetch(address, workflow_id))


async def _wait_closed(address: str, workflow_id: str, timeout: float) -> None:
    """等工作流结束（最多 timeout 秒；到时不报错，交给后面的检查）。"""
    client = await Client.connect(address)
    handle = client.get_workflow_handle(workflow_id)
    deadline = time.monotonic() + timeout
    while (await handle.describe()).status == WorkflowExecutionStatus.RUNNING and time.monotonic() < deadline:
        await asyncio.sleep(0.2)


def fetch_one(address: str, workflow_id: str, *, wait_closed: float = 30) -> WorkflowHistory:
    """一个 Task 工作流的历史。S-10 起 Task 先交付（task_finished）再收尾写回，客户端拿到结局时工作流可能还没结束：
    先等它结束再取。"""
    asyncio.run(_wait_closed(address, workflow_id, wait_closed))
    runs = fetch(address, workflow_id)
    assert len(runs) == 1, f"{workflow_id} 应只有一次运行，实际 {len(runs)} 次"
    return runs[0]


def _write(histories: Sequence[WorkflowHistory], directory: Path) -> None:
    for i, history in enumerate(histories):
        (directory / f"{history.workflow_id.replace('/', '_')}__{i}.json").write_text(history.to_json(), "utf-8")


def assert_replays_python(histories: Sequence[WorkflowHistory], *, env: dict[str, str] | None = None) -> None:
    """用 `python -m agentruntime_worker.replay` 回放；env 给出 ObjectStore 与 SecretStore，带 Claim-Check 引用的历史要用。"""
    with tempfile.TemporaryDirectory(prefix="task-histories-") as tmp:
        _write(histories, Path(tmp))
        proc = subprocess.run(
            [sys.executable, "-m", "agentruntime_worker.replay", "--dir", tmp],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})},
        )
    assert proc.returncode == 0, f"Task 工作流用当前代码回放失败：\n{proc.stdout}\n{proc.stderr}"


def assert_replays_go(task_service_bin: Path, histories: Sequence[WorkflowHistory], *, env: dict[str, str] | None = None) -> None:
    with tempfile.TemporaryDirectory(prefix="session-histories-") as tmp:
        _write(histories, Path(tmp))
        proc = subprocess.run(
            [str(task_service_bin), "replay", "--dir", tmp],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            env={**os.environ, **(env or {})},
        )
    assert proc.returncode == 0, f"Session 工作流用当前代码回放失败：\n{proc.stdout}\n{proc.stderr}"
