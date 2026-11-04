"""子进程树：命令可能再起子进程，超时与取消时都要一起停（docs/plan/running-task-interaction.md 第 2 节）。

POSIX 上让命令自成一个进程组，杀整个组；Windows 上用 taskkill /T。开发实现（本地沙箱、host 的 shell_run）用；K8s 与 Cloudflare 沙箱里的
agent 自己杀进程组（services/rs-sandbox-agent，D-28）。只用标准库。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from typing import Any


def new_group() -> dict[str, Any]:
    """asyncio.create_subprocess_exec 的参数：命令自成一组。"""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def kill_tree(process: asyncio.subprocess.Process) -> None:
    """杀掉命令和它的子进程，等它退出。直接的子进程已经退出、子进程还在时，POSIX 上还能按组杀；Windows 上找不到了。"""
    if sys.platform == "win32":
        if process.returncode is None:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/T", "/F", "/PID", str(process.pid), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await killer.wait()
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    await process.wait()
