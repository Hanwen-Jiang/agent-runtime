"""开发实现：内存 Cache。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from agentruntime_core.conformance.cache import CacheConformance
from agentruntime_core.dev.cache import InMemoryCacheProvider
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.testing import FakeClock, FaultInjector


class Harness:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.faults = FaultInjector()
        self.provider = InMemoryCacheProvider(clock=self.clock, faults=self.faults)

    async def advance(self, delta: timedelta) -> None:
        self.clock.advance(delta)
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)


class TestInMemoryCache(CacheConformance):
    @pytest.fixture
    def harness(self) -> Harness:
        return Harness()
