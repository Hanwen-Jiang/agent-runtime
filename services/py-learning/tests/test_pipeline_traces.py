"""T-16 第 1、2 步：读 trace（细化见 docs/plan/learning-pipeline.md）。

两种格式（OTLP JSON、控制台 JSON 行）解析成同样的 TaskTrace；只取指定租户；重复投递只算一次；缺属性的跳过并计数。
最后一条用真的 WrapUp 打出 span，再读回来：T-15 的打点与这里的解析是同一个契约。
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from agentruntime_learning.pipeline.traces import TaskTrace, TraceFormatError, read_spans, task_traces

from pipeline_support import FAMILY, ROUTE_A, ROUTE_B, SHAPE_A1, SHAPE_A2, console_lines, otlp_lines, route_task


def specs() -> list:
    return [
        route_task(1, ROUTE_A, status="done", cost=0.3),
        route_task(2, ROUTE_B, status="aborted", cost=0.05),
        route_task(3, ROUTE_A, status="done", cost=0.2, rerun=True),
        route_task(4, ROUTE_A, status="done", cost=0.2, tenant="t2"),
    ]


def by_id(tasks: list[TaskTrace]) -> dict[str, TaskTrace]:
    return {t.task_id: t for t in tasks}


@pytest.mark.parametrize("fmt", ["console", "otlp_json"])
def test_both_formats_give_the_same_task_traces(fmt: str) -> None:
    lines = console_lines(specs()) if fmt == "console" else otlp_lines(specs())
    result = task_traces(read_spans(lines, fmt=fmt), tenant_id="t1")
    tasks = by_id(result.tasks)
    assert len(tasks) == 3
    first = tasks[specs()[0].task_id]
    assert (first.tenant_id, first.task_family, first.status, first.recipe) == ("t1", FAMILY, "done", "")
    assert first.cost_usd == pytest.approx(0.3)
    assert [r.seq for r in first.rounds] == [1, 2]
    assert [r.verdict for r in first.rounds] == ["continue", "done"]
    assert [r.shape for r in first.rounds] == [SHAPE_A1, SHAPE_A2]
    assert all(r.planned for r in first.rounds)
    rerun = tasks[specs()[2].task_id]
    assert [r.planned for r in rerun.rounds] == [True, False, True], "原地重跑的 Round 没有 triage_level"


def test_the_two_formats_agree_exactly() -> None:
    a = task_traces(read_spans(console_lines(specs()), fmt="console"), tenant_id="t1")
    b = task_traces(read_spans(otlp_lines(specs(), per_line=1), fmt="otlp_json"), tenant_id="t1")
    assert sorted(a.tasks, key=lambda t: t.task_id) == sorted(b.tasks, key=lambda t: t.task_id)


def test_other_tenants_are_only_counted() -> None:
    result = task_traces(read_spans(console_lines(specs()), fmt="console"), tenant_id="t1")
    assert all(t.tenant_id == "t1" for t in result.tasks)
    assert result.other_tenant_spans == 3, "t2 的 Task 有一个 invoke_workflow 与两个 runtime.round"


def test_duplicate_delivery_counts_once() -> None:
    lines = console_lines(specs())
    once = by_id(task_traces(read_spans(lines, fmt="console"), tenant_id="t1").tasks)
    twice = task_traces(read_spans(lines + lines, fmt="console"), tenant_id="t1")
    assert by_id(twice.tasks) == once, "同一个 Task 的 span 投递两次，结果与一次相同（Round 不重复）"
    assert twice.duplicates == 3


def test_rounds_are_ordered_whatever_the_line_order() -> None:
    lines = console_lines(specs(), noise=False)
    result = task_traces(read_spans(list(reversed(lines)), fmt="console"), tenant_id="t1")
    for t in result.tasks:
        assert [r.seq for r in t.rounds] == sorted(r.seq for r in t.rounds)


def test_tasks_missing_key_attributes_are_skipped_and_counted() -> None:
    lines = console_lines(specs()[:2], noise=False)
    broken = json.loads(lines[0])
    del broken["attributes"]["runtime.task_family"]
    lines[0] = json.dumps(broken)
    orphan = {"kind": "span", "name": "runtime.round", "trace_id": "f" * 32, "span_id": "e" * 16, "parent_span_id": "d" * 16, "status": "ok", "events": [],
              "attributes": {"tenant_id": "t1", "round_seq": 1, "verdict": "done"}}
    result = task_traces(read_spans([*lines, json.dumps(orphan)], fmt="console"), tenant_id="t1")
    assert [t.task_id for t in result.tasks] == [specs()[1].task_id]
    assert result.skipped == 2, "缺任务族的 Task 与找不到所属 Task 的 Round 各算一次"


def test_round_shapes_that_are_not_json_skip_the_task() -> None:
    lines = console_lines(specs()[:1], noise=False)
    round_line = json.loads(lines[1])
    round_line["attributes"]["runtime.stage_shape"] = "{not json"
    lines[1] = json.dumps(round_line)
    result = task_traces(read_spans(lines, fmt="console"), tenant_id="t1")
    assert result.tasks == [] and result.skipped == 1


@pytest.mark.parametrize("fmt", ["console", "otlp_json"])
def test_malformed_lines_report_the_line_number(fmt: str) -> None:
    lines = (console_lines if fmt == "console" else otlp_lines)(specs()[:1])
    lines.insert(1, "{oops")
    with pytest.raises(TraceFormatError) as err:
        read_spans(lines, fmt=fmt)
    assert "第 2 行" in str(err.value)


def test_blank_lines_are_ignored() -> None:
    lines = console_lines(specs()[:1])
    assert read_spans(["", *lines, "   "], fmt="console") == read_spans(lines, fmt="console")


def test_unknown_format_is_rejected() -> None:
    with pytest.raises(ValueError):
        read_spans([], fmt="protobuf")


# --- 与 T-15 的打点是同一个契约 ---


async def test_spans_written_by_the_real_wrap_up_read_back(tmp_path: Path) -> None:
    from temporalio.testing import ActivityEnvironment

    from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
    from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
    from agentruntime_worker.activities.types import FinalizeTaskInput, Tenant
    from agentruntime_worker.reviewer.wrap_up import RecordingMemoryClient, WrapUp
    from agentruntime_worker.workflows import verdicts as vd

    stream = io.StringIO()
    telemetry = ConsoleTelemetryProvider(stream=stream)
    wrap = WrapUp(objects=LocalDiskObjectStoreProvider(tmp_path / "o", signing_key=os.urandom(32)), memory=RecordingMemoryClient(), telemetry=telemetry)
    stage1 = {"seq": 1, **SHAPE_A1}
    stage2 = {"seq": 2, **SHAPE_A2}
    tid = route_task(9, ROUTE_A).task_id
    inp = FinalizeTaskInput(
        tenant=Tenant(tenant_id="t1", user_id="u1", session_id="ses_01JA8Q2N4H6K8M0P2R4T6V8X0Z"),
        task_id=tid,
        status="done",
        task_state={"rounds": [
            {"seq": 1, "stage_goal": SHAPE_A1["goal"], "triage_level": "full", "status": "completed", "verdict": "continue", "cost_usd": 0.1},
            {"seq": 2, "stage_goal": SHAPE_A2["goal"], "triage_level": "full", "status": "completed", "verdict": "done", "cost_usd": 0.2},
        ], "pinned": {"recipe": ""}},
        verdict={"verdict": "done", "reason": "完成", "done": {"deliver_node": "check"}},
        deliver_node="check",
        stages=[vd.stage_summary(stage1), vd.stage_summary(stage2)],
        task_family=FAMILY,
    )
    await ActivityEnvironment().run(wrap.finalize, inp)
    result = task_traces(read_spans(stream.getvalue().splitlines(), fmt="console"), tenant_id="t1")
    [task] = result.tasks
    assert (task.task_id, task.task_family, task.status) == (tid, FAMILY, "done")
    assert task.cost_usd == pytest.approx(0.3)
    assert [r.shape for r in task.rounds] == [SHAPE_A1, SHAPE_A2]
