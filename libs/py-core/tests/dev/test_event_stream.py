"""开发实现：内存 EventStream。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from agentruntime_core.conformance.event_stream import EventStreamConformance
from agentruntime_core.dev.event_stream import InMemoryEventStreamProvider
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.testing import FakeClock, FaultInjector


class Harness:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.faults = FaultInjector()
        self.provider = InMemoryEventStreamProvider(clock=self.clock, faults=self.faults)

    async def advance(self, delta: timedelta) -> None:
        self.clock.advance(delta)
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)


class TestInMemoryEventStream(EventStreamConformance):
    @pytest.fixture
    def harness(self) -> Harness:
        return Harness()
