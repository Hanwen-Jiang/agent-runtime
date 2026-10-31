"""等模型的决策 activity 的心跳（docs/plan/running-task-interaction.md 第 2 节）：Temporal 的取消靠心跳送达，不发心跳的话打断送不到，
调用一直跑、一直花钱。间隔取 heartbeat 超时的 1/3（SDK 另按超时的 0.8 倍节流）。没有 heartbeat 超时的旧运行、activity 之外什么也不做。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from temporalio import activity


@contextlib.asynccontextmanager
async def heartbeating() -> AsyncIterator[None]:
    timeout = activity.info().heartbeat_timeout if activity.in_activity() else None
    if not timeout:
        yield
        return
    interval = timeout.total_seconds() / 3

    async def beat() -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(interval)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
