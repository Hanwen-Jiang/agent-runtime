"""S11（SBX-01 第 6 节第二行的故障注入）：Sandbox Service 被杀，借出中的环境按环境表恢复。

整套由 scripts/dev_stack.py 拉起（mock 供应商、桩决策、--sandbox local：Worker 经 Sandbox Service 用沙箱，本地后端每个环境一个
sandbox-agent 进程）。桩计划 shell 让一个 tool_loop 调 shell_run（inputs.stub_argv）：

1. 会话里第一个 Task 在环境里写一个文件；
2. 杀掉 Sandbox Service（SIGKILL）、再起来；
3. 同一会话的第二个 Task 读到这个文件，环境表里这个会话始终只有一个环境、仍是 ready（没有重建，Worker 看不出服务换过）。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from e2e_support.api import TaskApi, finished_payload, stream_until_finished
from e2e_support.business_db import psql, with_search_path
from e2e_support.devstack import DevStack

pytestmark = [pytest.mark.e2e, pytest.mark.faults]

WORKSPACE = "w-s11"


@pytest.fixture(scope="module")
def s11_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[DevStack]:
    root = tmp_path_factory.mktemp("s11-stack")
    s = DevStack.up(root, "--provider", "mock", "--decisions", "stub", "--sandbox", "local", "--tenant-effect", "t1=write_reversible")
    try:
        yield s
    finally:
        s.down()


def session_envs(stack: DevStack, session: str) -> list[tuple[str, str]]:
    """这个会话在环境表里的环境（env_ref、state）：SQLite 时读 <数据目录>/relational/t1.sqlite3，Postgres 时经 psql。"""
    if os.environ.get("AR_RELATIONAL_DSN"):
        dsn = with_search_path(os.environ["AR_RELATIONAL_DSN"], stack.business_db.schema)
        # 行级安全：先在这个会话里设租户（与 Postgres 适配器一样），否则看不到任何行。
        out = psql(dsn, "SELECT set_config('agentruntime.tenant_id', 't1', false); "
                        f"SELECT env_ref || '|' || state FROM sandbox_envs WHERE session_id = '{session}' ORDER BY created_at")
        return [tuple(line.split("|", 1)) for line in out.splitlines() if "|" in line]  # type: ignore[misc]
    db = stack.data_dir / "relational" / "t1.sqlite3"
    with sqlite3.connect(db) as conn:
        return [(r[0], r[1]) for r in conn.execute("SELECT env_ref, state FROM sandbox_envs WHERE session_id = ? ORDER BY created_at", (session,))]


def run_shell(api: TaskApi, session: str, script: str) -> dict[str, Any]:
    sub = api.submit("在沙箱里跑一条命令", stub_plan="shell", inputs={"stub_argv": ["sh", "-c", script]}, workspace_id=WORKSPACE, session_id=session)
    result = finished_payload(stream_until_finished(sub["eventsUrl"], api.token or "", timeout=180))
    assert result["status"] == "TASK_STATUS_DONE", result
    return sub


def test_killed_sandbox_service_keeps_the_session_environment(s11_stack: DevStack) -> None:
    stack = s11_stack
    api = TaskApi(stack.task_api, stack.token("t1", "u1"))
    (stack.workspace_root / "t1" / WORKSPACE).mkdir(parents=True, exist_ok=True)
    first = run_shell(api, "", "echo survived > s11.txt")
    session = first["sessionId"]
    before = session_envs(stack, session)
    assert len(before) == 1 and before[0][1] == "ready", before

    stack.kill("sandbox-service")
    stack.start("sandbox-service")

    run_shell(api, session, "cat s11.txt > s11-read.txt")
    assert (stack.workspace_root / "t1" / WORKSPACE / "s11-read.txt").read_text().strip() == "survived"
    assert session_envs(stack, session) == [(before[0][0], "ready")], "同一个环境，没有重建"
