"""开发实现：内存 VectorStore（精确检索）。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from agentruntime_core.conformance.vector_store import VectorStoreConformance
from agentruntime_core.dev.vector_store import InMemoryVectorStoreProvider
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.testing import FaultInjector


class Harness:
    def __init__(self) -> None:
        self.faults = FaultInjector()
        self.provider = InMemoryVectorStoreProvider(faults=self.faults)

    async def advance(self, delta: timedelta) -> None:
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)


class TestInMemoryVectorStore(VectorStoreConformance):
    @pytest.fixture
    def harness(self) -> Harness:
        return Harness()
