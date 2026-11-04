"""令牌不落到命令看得到的地方（D-28 验收 4；auth-tokens-2026-11-03.md 的 AGENT-TOK-01）。

命令与 agent 是同一个 uid（沙箱里都是 10001）：命令不能从 /proc/<agent>/environ、cmdline、mem 读到令牌。root 有 CAP_SYS_PTRACE，
能读任何进程的 /proc，所以测试进程是 root 时把 agent 以 nobody（65534）起，命令随之也是 nobody；测试进程不是 root 时照原样起。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import HELPER, TOKEN, Agent, b64, linux_only, start_agent, stop_agent

NOBODY = 65534


@pytest.fixture
def unprivileged_agent(exe: Path, impl: str) -> Iterator[tuple[Agent, str]]:
    root = Path(tempfile.mkdtemp(prefix="agent-tok-"))
    try:
        root.chmod(0o755)
        helper = root / "helper.py"
        shutil.copy(HELPER, helper)
        helper.chmod(0o644)
        work = root / "work"
        work.mkdir()
        user = None
        python = sys.executable
        if os.geteuid() == 0:
            user = NOBODY
            os.chown(work, NOBODY, NOBODY)
            python = shutil.which("python3", path="/usr/bin:/bin") or ""
            if not python:
                pytest.fail("以 root 跑这条要系统的 python3（给 nobody 用）")
        agent = start_agent(exe, work, impl=impl, user=user, extra_env={"PATH": "/usr/local/bin:/usr/bin:/bin"})
        try:
            yield agent, f"{python}\0{helper}"
        finally:
            stop_agent(agent)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@linux_only
def test_commands_cannot_read_the_agent_token_from_proc(unprivileged_agent: tuple[Agent, str]) -> None:
    agent, cmd = unprivileged_agent
    python, helper = cmd.split("\0")
    res = agent.exec([python, helper, "probe"])
    assert res.status == 200, res.body
    out = res.json()
    assert out["exit_code"] == 0, b64(out["stderr"])
    probe = json.loads(b64(out["stdout"]))
    assert probe["pid"] == agent.proc.pid, "探针的父进程要是 agent"
    assert TOKEN not in probe["environ"], "命令从 /proc/<agent>/environ 读到了 agent 的令牌"
    assert probe["environ_err"], f"/proc/<agent>/environ 不该让命令读：{probe['environ']!r}"
    assert TOKEN not in probe["cmdline"], "命令从 /proc/<agent>/cmdline 读到了 agent 的令牌"
    assert probe["mem_opened"] is False, "命令能打开 /proc/<agent>/mem，内存里的令牌就读得到"
    assert probe["self_readable"] is True, "命令自己的 /proc/self 要照常可读（只挡 agent，不挡命令）"


@linux_only
def test_the_token_is_not_in_the_commands_environment_or_on_disk(unprivileged_agent: tuple[Agent, str]) -> None:
    agent, cmd = unprivileged_agent
    python, helper = cmd.split("\0")
    names = json.loads(b64(agent.exec([python, helper, "env-names"]).json()["stdout"]))
    assert not [n for n in names if n.upper().startswith("AGENT_")]
    for p in agent.workdir.rglob("*"):
        if p.is_file():
            assert TOKEN.encode() not in p.read_bytes(), p
    assert TOKEN not in agent.logs(), "令牌不进日志"


def test_errors_and_logs_never_echo_the_token(agent: Agent) -> None:
    for res in (agent.request("GET", "/v1/manifest", token="x" * 40), agent.exec([], timeout_ms=1), agent.request("POST", "/v1/grep", b"{")):
        assert TOKEN.encode() not in res.body
    assert TOKEN not in agent.logs()
