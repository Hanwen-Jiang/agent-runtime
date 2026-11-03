"""PERF-00（含 WK-LITE-03）：Workspace 操作的分段耗时与进出 Worker 的字节数
（docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 2、7 节）。

1. 每次操作一个 runtime.workspace_op span：操作名、模式（archive、mount）、后端、结局（ok 或错误码），租户与工作区由绑定加上；
   分段的独占耗时 workspace.ms.<阶段>（acquire、restore、command、archive、release、other）加起来不超过 total。
2. 存档模式：从快照建的只读环境有 restore，可写的操作归还前有 archive；挂卷模式两者都没有。
3. 字节（worker.bytes.<通道>_<方向>，in 是进 Worker）：读文件时 sandbox_in 至少是文件的大小；存档模式下存档是 sandbox_in 与
   object_store_out，恢复是 object_store_in 与 sandbox_out；挂卷模式不碰 ObjectStore。
4. 指标：每个阶段（含 total）一条 runtime.workspace_op.duration_ms，每个（通道，方向）一条 runtime.worker.bytes；标签只有操作、
   阶段、模式、结局、通道、方向与租户，不带工作区、会话与用户（不随工作区膨胀）。
5. 失败的操作也记，结局是错误码；暂时性的错误照样抛给工作流的重试。
6. 属性与指标里没有路径与文件内容。
7. 经 activity 执行时记排队的耗时（这次尝试从排进队列到开始执行）。
8. shell_run 的 runtime.sandbox span 也记进出 Worker 的字节数，并记 runtime.worker.bytes（worker.activity 为 shell_run）。
9. 计时器：阶段嵌套时记独占时间；没开 Meter 时是空操作；嵌套开 Meter 时外层的照用。
"""

from __future__ import annotations

import dataclasses
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.errors import TransientError

from agentruntime_worker.workspace import timing
from agentruntime_worker.workspace.operations import WorkspaceActivities, WorkspaceOperations

from .test_workspace_archive import Setup as ArchiveSetup
from .test_workspace_operations import KEY, Op, Setup, make_remote

APP = b"def refund():\n    return 1\n"


def with_telemetry(tmp_path: Path, mode: str) -> tuple[Setup, ConsoleTelemetryProvider]:
    s = Setup(tmp_path, mode=mode)
    telemetry = ConsoleTelemetryProvider(stream=io.StringIO())

    async def secret() -> bytes:
        return KEY

    s.ops = WorkspaceOperations(s.broker, secret=secret, allow_local_remotes=True, telemetry=telemetry)
    return s, telemetry


async def spans(telemetry: ConsoleTelemetryProvider) -> list[dict[str, Any]]:
    await telemetry.bind_platform("test").flush()
    return [dict(s.attributes) for s in telemetry.exported_spans if s.name == "runtime.workspace_op"]


def metrics(telemetry: ConsoleTelemetryProvider, name: str) -> list[dict[str, Any]]:
    return [{**m.attributes, "value": m.value} for m in telemetry.exported_metrics if m.name == name]


def phases(span: dict[str, Any]) -> dict[str, int]:
    return {k.removeprefix("workspace.ms."): v for k, v in span.items() if k.startswith("workspace.ms.") and k != "workspace.ms.total"}


@pytest.mark.parametrize("mode", ["archive", "mount"])
async def test_each_operation_records_one_span_with_exclusive_phases(tmp_path: Path, mode: str) -> None:
    url, _ = make_remote(tmp_path)
    s, telemetry = with_telemetry(tmp_path, mode)
    await s.run(Op.WORKSPACE_OPERATION_CREATE, repo_url=url)
    await s.run(Op.WORKSPACE_OPERATION_LIST_FILES)
    await s.run(Op.WORKSPACE_OPERATION_COMMIT, message="空提交")
    got = await spans(telemetry)
    assert [a["workspace.operation"] for a in got] == ["create", "list_files", "commit"]
    for a in got:
        assert a["workspace.mode"] == mode and a["workspace.outcome"] == "ok" and a["sandbox.backend"] == "local", a
        assert a["tenant_id"] == "t1" and a["workspace_id"] == "w1"
        assert {"acquire", "command", "release", "other"} <= set(phases(a)), a
        assert sum(phases(a).values()) <= a["workspace.ms.total"] + len(phases(a)), "独占时间加起来不超过总时长（毫秒取整的误差除外）"
    create, listing, commit = got
    if mode == "archive":
        assert "archive" in phases(create) and "archive" in phases(commit), "可写的操作归还前存档"
        assert "restore" in phases(listing) and "restore" in phases(commit), "环境从快照恢复"
        assert "restore" not in phases(create), "还没有快照时没有恢复"
    else:
        assert not [a for a in got if {"restore", "archive"} & set(phases(a))], "挂卷模式不存档也不恢复"


@pytest.mark.parametrize("mode", ["archive", "mount"])
async def test_bytes_through_the_worker(tmp_path: Path, mode: str) -> None:
    url, _ = make_remote(tmp_path)
    s, telemetry = with_telemetry(tmp_path, mode)
    await s.run(Op.WORKSPACE_OPERATION_CREATE, repo_url=url)
    read = await s.run(Op.WORKSPACE_OPERATION_READ_FILE, path="app.py")
    assert read["content"] == APP.decode()
    create, rd = await spans(telemetry)
    assert rd["worker.bytes.sandbox_in"] >= len(APP), rd
    assert create.get("worker.bytes.sandbox_in", 0) > 0, "git 的输出也算"
    if mode == "archive":
        assert create["worker.bytes.sandbox_in"] > 0 and create["worker.bytes.object_store_out"] > 0, "存档：从环境导出、写块与清单"
        assert rd["worker.bytes.object_store_in"] > 0 and rd["worker.bytes.sandbox_out"] > 0, "恢复：读清单与块、导入环境"
    else:
        assert not [k for a in (create, rd) for k in a if k.startswith("worker.bytes.object_store")], "挂卷模式不碰 ObjectStore"


async def test_metrics_carry_only_low_cardinality_labels(tmp_path: Path) -> None:
    url, _ = make_remote(tmp_path)
    s, telemetry = with_telemetry(tmp_path, "archive")
    await s.run(Op.WORKSPACE_OPERATION_CREATE, repo_url=url)
    await s.run(Op.WORKSPACE_OPERATION_DIFF)
    got = await spans(telemetry)
    durations = metrics(telemetry, "runtime.workspace_op.duration_ms")
    counted = metrics(telemetry, "runtime.worker.bytes")
    assert {(m["workspace.operation"], m["workspace.phase"]) for m in durations} >= {
        ("create", "total"), ("create", "acquire"), ("create", "archive"), ("diff", "restore"), ("diff", "command"), ("diff", "other"),
    }
    diff_total = next(m for m in durations if m["workspace.operation"] == "diff" and m["workspace.phase"] == "total")
    assert diff_total["value"] == got[1]["workspace.ms.total"] and diff_total["workspace.mode"] == "archive" and diff_total["workspace.outcome"] == "ok"
    assert {(m["worker.channel"], m["worker.direction"]) for m in counted if m["workspace.operation"] == "diff"} >= {
        ("object_store", "in"), ("sandbox", "out"), ("sandbox", "in"),
    }
    assert all(m["worker.activity"] == "workspace_operation" for m in counted)
    for m in durations + counted:
        assert m["tenant_id"] == "t1"
        assert not {"workspace_id", "session_id", "user_id"} & set(m), m


async def test_failed_operations_are_recorded_with_the_error_code(tmp_path: Path) -> None:
    s, telemetry = with_telemetry(tmp_path, "mount")
    err = await s.error(Op.WORKSPACE_OPERATION_DIFF, workspace="nope")
    assert err.code == "workspace_not_found"
    await s.run(Op.WORKSPACE_OPERATION_CREATE, files=[{"path": "a.txt", "content": "a\n"}])

    async def unavailable(*_: Any, **__: Any) -> Any:
        raise TransientError("sandbox_unavailable", "沙箱后端都暂时不可用")

    s.broker.acquire = unavailable  # type: ignore[method-assign]
    with pytest.raises(TransientError):
        await s.ops.run(s.inp(Op.WORKSPACE_OPERATION_LIST_FILES))
    missing, _, transient = await spans(telemetry)
    assert missing["workspace.operation"] == "diff" and missing["workspace.outcome"] == "workspace_not_found"
    assert missing["workspace.mode"] == "none", "没借到环境的操作没有模式"
    assert transient["workspace.outcome"] == "sandbox_unavailable" and "workspace.ms.acquire" in transient
    statuses = [s.status for s in telemetry.exported_spans if s.name == "runtime.workspace_op"]
    assert statuses == ["ok", "ok", "error"], "交给重试的错误照样抛出，span 记为 error"


async def test_no_paths_or_contents_in_telemetry(tmp_path: Path) -> None:
    s, telemetry = with_telemetry(tmp_path, "archive")
    await s.run(Op.WORKSPACE_OPERATION_CREATE, files=[{"path": "plans/hidden-name-x7.md", "content": "CONTENT-MARKER-91\n"}])
    assert (await s.run(Op.WORKSPACE_OPERATION_READ_FILE, path="plans/hidden-name-x7.md"))["content"] == "CONTENT-MARKER-91\n"
    await s.run(Op.WORKSPACE_OPERATION_DIFF)
    await spans(telemetry)
    dump = json.dumps([dataclasses.asdict(r) for r in [*telemetry.exported_spans, *telemetry.exported_metrics]], ensure_ascii=False, default=str)
    assert "hidden-name-x7" not in dump and "CONTENT-MARKER-91" not in dump


async def test_activity_records_the_queue_time(tmp_path: Path) -> None:
    s, telemetry = with_telemetry(tmp_path, "mount")
    env = ActivityEnvironment()
    started = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)
    env.info = dataclasses.replace(env.info, current_attempt_scheduled_time=started - timedelta(milliseconds=2500), started_time=started)
    out = await env.run(WorkspaceActivities(s.ops).workspace_operation, s.inp(Op.WORKSPACE_OPERATION_CREATE, files=[{"path": "a.txt", "content": "a\n"}]))
    assert not out.HasField("error"), out.error
    (span,) = await spans(telemetry)
    assert span["workspace.ms.queue"] == 2500
    in_process = sum(v for k, v in phases(span).items() if k != "queue")
    assert in_process <= span["workspace.ms.total"] + len(phases(span)), "排队不在本进程里：不算进 total，也不从 other 里扣"
    queued = [m for m in metrics(telemetry, "runtime.workspace_op.duration_ms") if m["workspace.phase"] == "queue"]
    assert [m["value"] for m in queued] == [2500.0]


async def test_shell_run_records_worker_bytes(tmp_path: Path) -> None:
    s = ArchiveSetup(tmp_path)
    out = await s.py("print('x' * 5000)")
    assert out["exit_code"] == 0
    await s.py("print('again')")
    first, second = s.spans()
    assert first["worker.bytes.sandbox_in"] >= 5000, first
    assert second["worker.bytes.sandbox_in"] > 0
    counted = [m for m in (dict(r.attributes) for r in s.telemetry.exported_metrics if r.name == "runtime.worker.bytes") if m["worker.activity"] == "shell_run"]
    assert {(m["worker.channel"], m["worker.direction"]) for m in counted} >= {("sandbox", "in")}
    assert not [m for m in counted if {"workspace_id", "session_id", "user_id"} & set(m)]


def test_phases_are_exclusive_and_noop_without_a_meter() -> None:
    now = [0.0]
    with timing.phase("acquire"):
        now[0] += 1.0  # 不在任何 Meter 里：什么也不记
    meter = timing.Meter(clock=lambda: now[0])
    with timing.metering(meter) as m:
        assert m is meter
        with timing.phase("acquire"):
            now[0] += 0.2
            with timing.phase("restore"):
                now[0] += 0.5
            now[0] += 0.1
        with timing.metering() as inner:
            assert inner is meter, "嵌套时用外层的 Meter"
            with timing.phase("command"):
                now[0] += 0.3
        timing.add_bytes("sandbox", "in", 10)
        timing.add_bytes("sandbox", "in", 5)
        timing.add_bytes("object_store", "out", 0)
        now[0] += 0.4
    assert timing.current() is None
    attrs = meter.attributes()
    assert attrs["workspace.ms.acquire"] == 300 and attrs["workspace.ms.restore"] == 500 and attrs["workspace.ms.command"] == 300
    assert attrs["workspace.ms.other"] == 400 and attrs["workspace.ms.total"] == 1500
    assert attrs["worker.bytes.sandbox_in"] == 15 and "worker.bytes.object_store_out" not in attrs
