"""调用外部工具（buf、go、uv）的辅助函数。"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from contract_tests.paths import REPO_ROOT


def require_tool(name: str) -> str:
    """返回工具的路径；找不到时测试失败。

    本地临时缺工具时可设 AR_ALLOW_MISSING_TOOLS=1 改为跳过；CI 不设，缺工具即失败。
    """
    path = shutil.which(name)
    if path:
        return path
    message = f"{name} 不在 PATH 上；S-01 的契约测试需要它"
    if os.environ.get("AR_ALLOW_MISSING_TOOLS") == "1":
        pytest.skip(message)
    pytest.fail(message, pytrace=False)


def run(
    cmd: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path = REPO_ROOT,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    args = [os.fspath(part) for part in cmd]
    proc = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        pytest.fail(
            f"命令失败（退出码 {proc.returncode}）：{' '.join(args)}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
            pytrace=False,
        )
    return proc
