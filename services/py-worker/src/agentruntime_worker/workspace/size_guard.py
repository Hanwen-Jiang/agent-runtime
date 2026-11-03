"""挂卷工作区的大小上限（DISK-A，WS-DISK-01 ①；docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 2 节）。

挂卷模式不存档，原来没有任何大小限制：一个租户就能把节点的盘写满，连带同节点上的数据库。这里在写类工具（repo_edit、shell_run）
前后核对租户策略的 workspace.max_bytes：

- 写完量一次工作区（逐个 lstat，不跟随符号链接，超过上限就停）；超了在这次的结果里带上 workspace_size，告诉调用方与用户。
- 记着超了的工作区之后的写一律拒绝（denied，workspace_too_large）；每次被拒之前先重新量，清理过（例如经 Workspace API
  丢弃改动）就放行。
- 量大小要扫目录，大仓库有开销：没超时两次测量至少隔 interval 秒（同一个 Worker 进程内，按工作区记）。

存档模式不在这里管：存档时已核对 max_bytes（workspace/archive.py）。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentruntime_core.errors import DeniedError

# 读一个租户的工作区大小上限（字节）。
LimitLookup = Callable[[str], Awaitable[int]]


@dataclass
class _Measured:
    at: float
    size: int
    over: bool


def measure(root: Path, stop_above: int) -> int:
    """工作区里文件的字节数（lstat，不跟随符号链接，不进链接指向的目录）；超过 stop_above 就停，返回已数到的。"""
    total = 0
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    else:
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                if total > stop_above:
                    return total
    return total


class WorkspaceSizeGuard:
    def __init__(self, limit: LimitLookup, *, interval: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._interval = interval
        self._clock = clock
        self._state: dict[tuple[str, str], _Measured] = {}

    async def _measure(self, tenant_id: str, workspace_id: str, root: Path) -> tuple[_Measured, int]:
        limit = await self._limit(tenant_id)
        size = await asyncio.to_thread(measure, root, limit)
        state = _Measured(at=self._clock(), size=size, over=size > limit)
        self._state[(tenant_id, workspace_id)] = state
        return state, limit

    async def before_write(self, tenant_id: str, workspace_id: str, root: Path) -> None:
        """写之前：上次量下来超了就重新量，仍然超就拒绝。"""
        state = self._state.get((tenant_id, workspace_id))
        if state is None or not state.over:
            return
        state, limit = await self._measure(tenant_id, workspace_id, root)
        if state.over:
            raise DeniedError("workspace_too_large", _message(workspace_id, state.size, limit, refused=True))

    async def after_write(self, tenant_id: str, workspace_id: str, root: Path) -> dict[str, Any] | None:
        """写之后：到了测量间隔就量一次；超了返回要并进结果的 workspace_size，否则 None。"""
        state = self._state.get((tenant_id, workspace_id))
        if state is not None and not state.over and self._clock() - state.at < self._interval:
            return None
        state, limit = await self._measure(tenant_id, workspace_id, root)
        if not state.over:
            return None
        return {"bytes": state.size, "max_bytes": limit, "over_limit": True, "message": _message(workspace_id, state.size, limit, refused=False)}


def _message(workspace_id: str, size: int, limit: int, *, refused: bool) -> str:
    head = f"工作区 {workspace_id} 至少有 {size} 字节，超过上限 {limit} 字节（租户策略的 workspace.max_bytes）"
    tail = "：写类工具（repo_edit、shell_run）暂停，先清理（经 Workspace API 丢弃改动、删掉不要的文件，或请管理员调高上限）再继续"
    return head + ("，这次写被拒" if refused else "") + tail


__all__ = ["LimitLookup", "WorkspaceSizeGuard", "measure"]
