"""开发实现与一致性测试共用的测试支持：可拨动的时钟与故障注入。

开发实现（内存、SQLite、本地磁盘等）在构造时接收 clock 与 faults，一致性测试的 harness 用它们
拨动时间、注入 transient 等错误。生产实现不需要它们。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

from agentruntime_core.errors import ErrorCategory, ProviderError


class FakeClock:
    """只在 advance() 时前进的时钟。sleep() 等到时钟被拨过目标时间才返回。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 1, tzinfo=UTC)
        self._waiters: list[tuple[datetime, asyncio.Future[None]]] = []

    def now(self) -> datetime:
        return self._now

    async def sleep(self, delay: timedelta) -> None:
        if delay <= timedelta(0):
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((self._now + delay, future))
        await future

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("时钟不能倒退")
        self._now += delta
        due = [(t, f) for t, f in self._waiters if t <= self._now]
        self._waiters = [(t, f) for t, f in self._waiters if t > self._now]
        for _, future in due:
            if not future.done():
                future.set_result(None)


class FaultInjector:
    """按操作名注入错误。开发实现在每个公开方法开头调用 check(操作名)。"""

    def __init__(self) -> None:
        self._pending: defaultdict[str, deque[ProviderError]] = defaultdict(deque)

    def fail_next(self, operation: str, category: ErrorCategory, *, times: int = 1, code: str = "injected") -> None:
        for _ in range(times):
            self._pending[operation].append(ProviderError.of(category, code, f"注入的故障：{operation}"))

    def check(self, operation: str) -> None:
        queue = self._pending.get(operation)
        if queue:
            raise queue.popleft()
