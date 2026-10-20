"""S-03 验收“重放”：所有集成测试导出的历史，都能被当前代码回放。

本模块排在最后（conftest.py）：导出前面各个用例跑出来的全部工作流事件历史，Session 工作流的交给
`task-service replay`（Go），Task 工作流的交给 `python -m agentruntime_worker.replay`（Python）。

设环境变量 AR_UPDATE_GOLDEN_HISTORIES=1 时，另从中挑出覆盖续接、取消、人工回复等情形的几份，
写进两边的 golden 目录（internal/session/testdata/histories 与 py-worker/tests/histories），
供单元级的重放测试与“调换命令顺序必须失败”的测试使用。更新 golden 是有意识的操作，结果要提交并人审。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from temporalio.client import Client

from e2e_support.stack import REPO_ROOT, Stack

pytestmark = pytest.mark.e2e

GO_GOLDEN = REPO_ROOT / "services" / "go-task-service" / "internal" / "session" / "testdata" / "histories"
PY_GOLDEN = REPO_ROOT / "services" / "py-worker" / "tests" / "histories"

# golden 要覆盖的情形：名字 → 判断事件历史是否属于这种情形的正则。按从具体到一般排列，
# 一份历史只给一个情形，具体的情形先挑，免得被一般的情形占掉。
SESSION_CASES = {
    "continue_as_new": r"(?i)continued_?as_?new",
    "cancel_running": r"(?i)request_?cancel_?external",
    "plain": r"(?i)start_?child_?workflow",
}
TASK_CASES = {
    "human_reply": r'(?i)"signalName":\s*"human_reply"',
    "cancelled": r"(?i)workflow_?execution_?cancel_?requested",
    "completed": r"(?i)workflow_?execution_?completed",
}


async def export_all(address: str, out: Path) -> dict[str, list[Path]]:
    client = await Client.connect(address)
    exported: dict[str, list[Path]] = {}
    async for wf in client.list_workflows():
        history = await client.get_workflow_handle(wf.id, run_id=wf.run_id).fetch_history()
        directory = out / wf.workflow_type
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{wf.id}__{wf.run_id}.json"
        path.write_text(history.to_json(), "utf-8")
        exported.setdefault(wf.workflow_type, []).append(path)
    return exported


def pick(files: list[Path], cases: dict[str, str]) -> dict[str, Path]:
    chosen: dict[str, Path] = {}
    for name, pattern in cases.items():
        match = next((f for f in sorted(files) if re.search(pattern, f.read_text("utf-8")) and f not in chosen.values()), None)
        assert match is not None, f"导出的事件历史里没有“{name}”的情形"
        chosen[name] = match
    return chosen


def test_every_exported_history_replays(stack: Stack, tmp_path: Path) -> None:
    exported = asyncio.run(export_all(stack.temporal_address, tmp_path))
    assert exported.get("SessionWorkflow"), "没有导出 Session 工作流的历史"
    assert exported.get("TaskWorkflow"), "没有导出 Task 工作流的历史"

    assert stack.task_service_bin is not None
    # 带 Claim-Check 引用的历史（S-06）要从共用的 ObjectStore 取回对象，回放工具与服务用同样的配置。
    go = subprocess.run(
        [str(stack.task_service_bin), "replay", "--dir", str(tmp_path / "SessionWorkflow")],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        timeout=600,
        env={**os.environ, **stack.shared_store_env()},
    )
    assert go.returncode == 0, f"Go 回放失败：\n{go.stdout}\n{go.stderr}"

    py = subprocess.run(
        [sys.executable, "-m", "agentruntime_worker.replay", "--dir", str(tmp_path / "TaskWorkflow")],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        timeout=600,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", **stack.shared_store_env()},
    )
    assert py.returncode == 0, f"Python 回放失败：\n{py.stdout}\n{py.stderr}"

    session_golden = pick(exported["SessionWorkflow"], SESSION_CASES)
    task_golden = pick(exported["TaskWorkflow"], TASK_CASES)
    if os.environ.get("AR_UPDATE_GOLDEN_HISTORIES") == "1":
        # 只补缺的 golden，不覆盖、不删除已有的：已有的 golden 是旧版本代码留下的历史，用来证明它们仍能回放；
        # py-worker 的目录里还有 Stage Runner 等其他测试写的 golden。要重新生成某一份，先删掉它。
        for directory, chosen in ((GO_GOLDEN, session_golden), (PY_GOLDEN, task_golden)):
            directory.mkdir(parents=True, exist_ok=True)
            for name, path in chosen.items():
                target = directory / f"{name}.json"
                if not target.exists():
                    shutil.copyfile(path, target)
