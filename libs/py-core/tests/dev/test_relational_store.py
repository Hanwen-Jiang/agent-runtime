"""开发实现：SQLite RelationalStore（每个作用域一个库）。"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from agentruntime_core.conformance.relational_store import RelationalStoreConformance
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.testing import FaultInjector


class Harness:
    def __init__(self, directory: Path) -> None:
        self.faults = FaultInjector()
        self.provider = SQLiteRelationalStoreProvider(directory, faults=self.faults)

    async def advance(self, delta: timedelta) -> None:
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)


class TestSQLiteRelationalStore(RelationalStoreConformance):
    @pytest.fixture
    async def harness(self, tmp_path: Path):
        harness = Harness(tmp_path)
        yield harness
        await harness.provider.aclose()
