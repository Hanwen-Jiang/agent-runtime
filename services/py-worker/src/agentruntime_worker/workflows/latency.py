"""首事件延迟（docs/plan/feedback-2026-11-01.md N-20；人工验收基线 Q59；docs/plan/n19-n20-contract-baseline.md）。

口径：请求接收（Task Service 记的 submitted_at）→ 第一条可展示事件。拆成几段：

- queue：submitted_at → Task 工作流开始跑（Session 里排队，加上 Worker 取到工作流任务的时间）；
- planning：第一轮规划（plan_round，含分诊与重试）的用时；
- sandbox_cold_start：第一次新建沙箱环境借到环境的用时（tool_status 的 sandbox_startup_ms）；
- first_token：第一个模型 token（只有流式通道才有；没有时是 unknown，不伪造）；
- first_event：submitted_at → 第一条可展示事件（总数）。

缺的段记为 unknown。时间都是工作流时间（workflow.now()），submitted_at 是 Task Service 的时钟，两者来源不同（基线已列为新的失败面）。
只记时间戳，不改命令序列：收尾时随 finalize_task 的输入交给 activity，由它记 span 与指标（reviewer/wrap_up.py）。

是工作流代码：时间由调用方传入（workflow.now()）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# 可展示的事件：客户端会画出内容的那些（round_started、decision_retry、context_usage、guidance_updated 只是状态）。
DISPLAYABLE = frozenset({
    "plan_updated", "stage_started", "node_started", "tool_status", "human_input_required", "verdict", "token_delta", "thinking_delta",
})
SEGMENTS = ("queue", "planning", "sandbox_cold_start", "first_token", "first_event")
UNKNOWN = "unknown"


def _ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, round((end - start).total_seconds() * 1000))


class FirstEventLatency:
    """一个 Task 的首事件延迟；只在顶层 Task 的工作流里用。"""

    def __init__(self, accepted_at: datetime | None, started_at: datetime) -> None:
        self.accepted_at = accepted_at
        self.started_at = started_at
        self.planning_started: datetime | None = None
        self.planning_finished: datetime | None = None
        self.first_event_at: datetime | None = None
        self.first_event = ""
        self.sandbox_cold_start_ms: int | None = None
        self.first_token_ms: int | None = None

    def planning(self, started: datetime, finished: datetime) -> None:
        """只记第一轮的规划。"""
        if self.planning_started is None:
            self.planning_started, self.planning_finished = started, finished

    def event(self, name: str, at: datetime) -> None:
        if self.first_event_at is None and name in DISPLAYABLE:
            self.first_event_at, self.first_event = at, name

    def finished(self, at: datetime) -> None:
        """收尾时还没有可展示的事件：第一条就是 Session 推的 task_finished。"""
        if self.first_event_at is None:
            self.first_event_at, self.first_event = at, "task_finished"

    def sandbox(self, startup_ms: int) -> None:
        if self.sandbox_cold_start_ms is None:
            self.sandbox_cold_start_ms = startup_ms

    def as_dict(self) -> dict[str, Any]:
        """{"first_event": 事件名, "segments_ms": {段: 毫秒或 "unknown"}}：finalize_task 的输入。"""
        values = {
            "queue": _ms(self.accepted_at, self.started_at),
            "planning": _ms(self.planning_started, self.planning_finished),
            "sandbox_cold_start": self.sandbox_cold_start_ms,
            "first_token": self.first_token_ms,
            "first_event": _ms(self.accepted_at, self.first_event_at),
        }
        return {"first_event": self.first_event or UNKNOWN, "segments_ms": {k: UNKNOWN if values[k] is None else values[k] for k in SEGMENTS}}
