"""docs/plan/feedback-2026-11-01.md N-20：首事件延迟拆段计量（人工验收基线 Q59；契约清单 latency_segments、cold_start_is_reused）。

口径是请求接收 → 第一条可展示事件；各段（排队、规划、沙箱冷启动、首个 token）进 trace 与指标，缺的记 unknown，不伪造。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_worker.activities.types import ToolLoopStep
from agentruntime_worker.reviewer.wrap_up import RecordingMemoryClient, WrapUp
from agentruntime_worker.workflows.latency import SEGMENTS, FirstEventLatency

from .reviewer_support import FakeReviewGateway
from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .test_reviewer_activity import RecordingTelemetry, activities, finalize_input
from .test_stage_runner import deliver, goal, one_round, scripted_steps

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

T0 = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)


def at(ms: int) -> datetime:
    return T0 + timedelta(milliseconds=ms)


def test_segments_from_acceptance_to_the_first_displayable_event() -> None:
    lat = FirstEventLatency(T0, at(400))
    lat.event("round_started", at(450))  # 只是状态，不算
    lat.planning(at(450), at(2450))
    lat.planning(at(9000), at(9500))  # 只记第一轮
    lat.event("plan_updated", at(2460))
    lat.event("stage_started", at(2470))
    lat.sandbox(1800)
    lat.sandbox(10)  # 只记第一次冷启动
    out = lat.as_dict()
    assert out == {
        "first_event": "plan_updated",
        "segments_ms": {"queue": 400, "planning": 2000, "sandbox_cold_start": 1800, "first_token": "unknown", "first_event": 2460},
    }


def test_missing_segments_are_unknown_and_task_finished_is_the_fallback_event() -> None:
    lat = FirstEventLatency(None, at(0))
    lat.finished(at(300))
    out = lat.as_dict()
    assert out["first_event"] == "task_finished"
    assert all(out["segments_ms"][s] == "unknown" for s in ("queue", "planning", "sandbox_cold_start", "first_token", "first_event"))
    assert tuple(out["segments_ms"]) == SEGMENTS


async def test_the_task_hands_its_first_event_latency_to_the_wrap_up(harness: StageHarness) -> None:
    s = harness.script
    s.tools.update({"shell_run": "read"})
    s.tool_steps["explore"] = scripted_steps(
        ToolLoopStep(action="tool", tool="shell_run", args={"argv": ["ls"]}),
        ToolLoopStep(action="finish", result={"ok": True}),
    )
    s.tool_results["shell_run"] = lambda i: {"exit_code": 0, "stdout": "a", "sandbox_startup_ms": 1234}
    one_round(harness, goal("explore", "tool_loop", tools=["shell_run"]), deliver(after=["explore"]))
    inp, _ = await harness.run()
    latency = next(f for f in s.finalized if f.task_id == inp.task_id).latency
    seg = latency["segments_ms"]
    assert latency["first_event"] == "stage_started", "没有 Task Plan 的步骤时，第一条可展示的是 stage_started"
    assert isinstance(seg["queue"], int) and isinstance(seg["planning"], int) and isinstance(seg["first_event"], int)
    assert seg["first_event"] >= seg["queue"]
    assert seg["sandbox_cold_start"] == 1234, "冷启动取 tool_status 的 sandbox_startup_ms"
    assert seg["first_token"] == "unknown", "没有流式通道时不伪造"


async def test_reused_sandboxes_leave_the_cold_start_unknown(harness: StageHarness) -> None:
    s = harness.script
    s.tools.update({"shell_run": "read"})
    s.tool_steps["explore"] = scripted_steps(
        ToolLoopStep(action="tool", tool="shell_run", args={"argv": ["ls"]}),
        ToolLoopStep(action="finish", result={"ok": True}),
    )
    s.tool_results["shell_run"] = lambda i: {"exit_code": 0, "stdout": "a"}
    one_round(harness, goal("explore", "tool_loop", tools=["shell_run"]), deliver(after=["explore"]))
    inp, _ = await harness.run()
    assert next(f for f in s.finalized if f.task_id == inp.task_id).latency["segments_ms"]["sandbox_cold_start"] == "unknown"


async def test_sub_tasks_without_events_record_no_latency(harness: StageHarness) -> None:
    one_round(harness, deliver("answer"))
    inp, _ = await harness.run(depth=1)
    assert next(f for f in harness.script.finalized if f.task_id == inp.task_id).latency == {}


async def test_the_wrap_up_records_a_span_and_metrics_for_known_segments(tmp_path: Path) -> None:
    import os

    telemetry = RecordingTelemetry()
    wrap = WrapUp(objects=LocalDiskObjectStoreProvider(tmp_path / "o", signing_key=os.urandom(32)), memory=RecordingMemoryClient(), telemetry=telemetry)
    latency: dict[str, Any] = {
        "first_event": "plan_updated",
        "segments_ms": {"queue": 400, "planning": 2000, "sandbox_cold_start": "unknown", "first_token": "unknown", "first_event": 2460},
    }
    await ActivityEnvironment().run(activities(FakeReviewGateway([]), wrap_up=wrap).finalize_task, finalize_input(latency=latency))
    [span] = [p for p in telemetry.points if p[1] == "span" and p[2] == "runtime.task.first_event"]
    assert span[0] == "t1" and span[4]["runtime.first_event"] == "plan_updated"
    assert span[4]["runtime.latency.planning_ms"] == 2000 and span[4]["runtime.latency.first_token_ms"] == "unknown"
    observed = {p[4]["segment"]: p[3] for p in telemetry.points if p[1] == "observe" and p[2] == "runtime.task.first_event_ms"}
    assert observed == {"queue": 400.0, "planning": 2000.0, "first_event": 2460.0}, "unknown 的段不记指标"
