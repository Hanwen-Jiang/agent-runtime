"""T-13 验收（真模型端到端，docs/plan/live-e2e.md 第 5 节）：CI 修复从 Ingress 到交付，以及 tool_loop 中途杀 Worker 后恢复。

样本仓库 workspaces/ci_rounding：金额“半分进一”写成了 Python 的 round()（银行家舍入），五条 unittest 里三条失败。
真 Planner、经网关的 tool_loop、真 Reviewer，工具是平台内的 repo_*、shell_run。只断言能独立核对的结果（第 4 节“真模型的随机性”）：
结局、测试自己再跑一遍 unittest、测试文件没被改、事件历史干净且能回放；不断言 Stage 形状、Round 数或步数。
每条用例结束写一份端到端日志（tests/e2e/.live-reports/<时间>/，不提交）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from e2e_support import history as h
from e2e_support.api import SseEvent, TaskApi, finished_payload, stream_until_finished
from e2e_support.devstack import LIVE_TENANT, LIVE_USER, DevStack
from e2e_support.live_report import write_run_report
from e2e_support.stack import REPO_ROOT, wait_until

pytestmark = pytest.mark.e2e

SAMPLE = Path(__file__).parent / "workspaces" / "ci_rounding"
TEST_FILE = "test_pricing.py"
MESSAGE = (
    "CI 挂了：在仓库根目录运行 `python -m unittest` 有用例失败。请找出原因并修好 pricing.py，让全部测试通过；"
    "不要改测试文件 test_pricing.py。改完再运行一遍 `python -m unittest` 确认。"
)
# 第 3 节第 4 条：每条用例按网关计量的标价合计不超过这么多美元。
COST_LIMIT_USD = 3.0
TASK_TIMEOUT_S = 1500
REPORTS_DIR = REPO_ROOT / "tests" / "e2e" / ".live-reports"
PROGRESS_EVENTS = {"task_accepted", "round_started", "stage_started", "verdict", "task_finished"}
# 修 CI 至少要读代码、改代码、跑测试。
REQUIRED_TOOLS = {"repo_read", "repo_edit", "shell_run"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unittest_in(workspace: Path) -> subprocess.CompletedProcess[str]:
    """测试自己在工作区里跑一遍 unittest（不信 Task 的自述）。"""
    return subprocess.run(
        [sys.executable, "-B", "-m", "unittest"],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def submit(live: DevStack, workspace: str) -> tuple[TaskApi, dict[str, Any]]:
    """经 Ingress（Task API）提交；inputs 不带桩的 stub_plan。"""
    api = TaskApi(live.task_api, live.token(LIVE_TENANT, LIVE_USER))
    sub = api.call(
        "SubmitTask",
        {
            "client_request_id": uuid.uuid4().hex,
            "message": MESSAGE,
            "workspace_id": workspace,
            "session_access": "SESSION_ACCESS_READ_WRITE",
        },
    )
    return api, sub


def task_records(live: DevStack, task_id: str) -> list[dict[str, Any]]:
    return [r for r in live.metering() if r.get("task_id") == task_id]


def cost_usd(records: list[dict[str, Any]]) -> float:
    return sum(int(r.get("cost_usd_micros") or 0) for r in records) / 1_000_000


def assert_no_secret(live: DevStack, *texts: str) -> None:
    """不用 assert key not in ...：失败时 pytest 会把密钥打印出来。"""
    haystack = "\n".join([live.all_output(), *texts])
    if any(s and s in haystack for s in live.secrets):
        pytest.fail("进程输出、日志或端到端日志里出现了供应商的密钥（已隐去）", pytrace=False)


def check_events(events: list[SseEvent]) -> None:
    names = [e.event for e in events]
    assert PROGRESS_EVENTS <= set(names), f"SSE 应看得到进度（T-11b）：{names}"
    assert [e.id for e in events] == list(range(1, len(events) + 1)), "event_id 从 1 起连续递增"
    assert '"rationale"' not in json.dumps([e.data for e in events], ensure_ascii=False), "rationale 只写 trace，不下传（设计书 3.2.4）"


def check_delivery(api: TaskApi, sub: dict[str, Any], events: list[SseEvent], workspace: Path, original_test: str) -> dict[str, Any]:
    result = finished_payload(events)
    assert result["status"] == "TASK_STATUS_DONE", f"Task 没有交付：{json.dumps(result, ensure_ascii=False)[:2000]}"
    assert api.query(sub["taskId"])["status"] == "TASK_STATUS_DONE"
    run = unittest_in(workspace)
    assert run.returncode == 0, f"交付之后工作区里的 unittest 仍失败：\n{run.stderr[-3000:]}"
    assert digest(workspace / TEST_FILE) == original_test, "不许改测试文件"
    return result


def check_model_calls(live: DevStack, task_id: str) -> list[dict[str, Any]]:
    records = task_records(live, task_id)
    assert records, "Task 的模型调用都经 Model Gateway"
    assert {r["tenant_id"] for r in records} == {LIVE_TENANT}, "计量都记在 Task 的租户名下"
    nodes = {str(r.get("node_id")) for r in records}
    assert nodes & {"triage", "plan"}, f"真 Planner 经网关（triage 或 plan）：{sorted(nodes)}"
    assert "review" in nodes, f"真 Reviewer 经网关：{sorted(nodes)}"
    assert any("." in n for n in nodes), f"tool_loop 的每一步经网关（node_id 是 <Goal>.<序号>）：{sorted(nodes)}"
    spent = cost_usd(records)
    assert 0 < spent <= COST_LIMIT_USD, f"这条用例按标价花了 ${spent:.4f}，上限 ${COST_LIMIT_USD}"
    return records


def check_history(live: DevStack, task_id: str, *, worker_restarted: bool) -> tuple[Any, h.HistoryReport]:
    task = h.fetch_one(live.temporal, task_id, wait_closed=120)
    report = h.analyze(task)
    h.assert_clean(report, worker_restarted=worker_restarted)
    used = {str((a.input or {}).get("tool")) for a in h.tool_calls(report) if a.outcome == "completed"}
    assert REQUIRED_TOOLS <= used, f"用到了真工具（读、改、跑测试）：{sorted(used)}"
    assert report.scheduled("plan_round") and report.scheduled("review_round") and report.scheduled("tool_loop_step")
    h.assert_replays_python([task], env=live.shared_env)
    return task, report


def report_dir(name: str) -> Path:
    return REPORTS_DIR / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{name}"


def write_and_check_report(live: DevStack, name: str, sub: dict[str, Any], events: list[SseEvent], report: h.HistoryReport, records: list[dict[str, Any]], extra: dict[str, Any]) -> None:
    path = write_run_report(
        report_dir(name),
        name=name,
        task_id=sub["taskId"],
        events=events,
        history=report,
        metering=records,
        extra=extra,
        secrets=live.secrets,
    )
    run_md, run_json = path / "run.md", path / "run.json"
    assert run_md.exists() and run_json.exists(), "端到端日志：run.md（时间线）与 run.json"
    data = json.loads(run_json.read_text("utf-8"))
    for key in ("task_id", "status", "events", "activities", "tool_calls", "metering", "cost_usd", "cache_read_ratio"):
        assert key in data, f"run.json 缺 {key}"
    assert_no_secret(live, run_md.read_text("utf-8"), run_json.read_text("utf-8"))


def test_ci_fix_task_is_delivered_end_to_end(live: DevStack) -> None:
    workspace = live.seed_workspace(LIVE_TENANT, "t13-ci", SAMPLE)
    original_test = digest(workspace / TEST_FILE)
    assert unittest_in(workspace).returncode != 0, "样本仓库开始时 CI 是红的"

    api, sub = submit(live, "t13-ci")
    events = stream_until_finished(sub["eventsUrl"], api.token or "", timeout=TASK_TIMEOUT_S)
    check_delivery(api, sub, events, workspace, original_test)
    check_events(events)
    records = check_model_calls(live, sub["taskId"])
    _, report = check_history(live, sub["taskId"], worker_restarted=False)
    assert_no_secret(live)
    write_and_check_report(live, "ci-fix", sub, events, report, records, {"worker_killed": False})


def test_worker_killed_mid_tool_loop_recovers(live: DevStack) -> None:
    workspace = live.seed_workspace(LIVE_TENANT, "t13-kill", SAMPLE)
    original_test = digest(workspace / TEST_FILE)

    api, sub = submit(live, "t13-kill")
    task_id = sub["taskId"]

    def a_tool_call_completed() -> list[str]:
        report = h.analyze(h.fetch(live.temporal, task_id)[-1])
        return [str(a.node_id) for a in h.tool_calls(report) if a.outcome == "completed"]

    # 第 3 节第 6 条：至少一次工具调用完成之后、Task 结束之前强杀。
    done_before = wait_until(a_tool_call_completed, "tool_loop 里至少一次工具调用完成", timeout=TASK_TIMEOUT_S, interval=2)
    assert api.query(task_id)["status"] == "TASK_STATUS_RUNNING", "杀 Worker 时 Task 还在跑"
    live.kill("worker")
    time.sleep(2)
    live.start("worker")

    events = stream_until_finished(sub["eventsUrl"], api.token or "", timeout=TASK_TIMEOUT_S)
    check_delivery(api, sub, events, workspace, original_test)
    check_events(events)
    records = check_model_calls(live, task_id)
    _, report = check_history(live, task_id, worker_restarted=True)

    # 恢复后不重复：杀之前完成的工具调用还是那一次（第一次 attempt 完成），没有再调度；之后的调用 id 各不相同。
    calls = {str(a.node_id): a for a in h.tool_calls(report)}
    for call_id in done_before:
        assert calls[call_id].outcome == "completed" and calls[call_id].attempts == 1, f"杀之前完成的 {call_id} 不应重跑"
    ids = [str(a.node_id) for a in h.tool_calls(report)]
    assert len(ids) == len(set(ids)), "每次工具调用只调度一次"
    assert_no_secret(live)
    write_and_check_report(live, "ci-fix-kill", sub, events, report, records, {"worker_killed": True, "completed_before_kill": done_before})
