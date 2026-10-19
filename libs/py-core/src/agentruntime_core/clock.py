"""时钟。有过期语义的开发实现（Cache、ObjectStore、SecretStore 等）都从这里取时间，测试可以换成 FakeClock。

注意：这是活动侧与服务侧代码用的时钟。Temporal 工作流代码必须用 workflow.now()，不能用它。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """带时区的 UTC 时间。"""
        ...

    async def sleep(self, delay: timedelta) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, delay: timedelta) -> None:
        await asyncio.sleep(max(delay.total_seconds(), 0))
