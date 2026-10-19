"""开发实现：本地磁盘 ObjectStore。"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from agentruntime_core.conformance.object_store import ObjectStoreConformance
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.testing import FakeClock, FaultInjector


class Harness:
    def __init__(self, root: Path) -> None:
        self.clock = FakeClock()
        self.faults = FaultInjector()
        self.provider = LocalDiskObjectStoreProvider(
            root, signing_key=b"conformance-signing-key", clock=self.clock, faults=self.faults
        )

    async def advance(self, delta: timedelta) -> None:
        self.clock.advance(delta)
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)

    async def fetch(self, url: str) -> bytes:
        return await self.provider.fetch_url(url)

    async def upload(self, url: str, data: bytes) -> None:
        await self.provider.upload_url(url, data)

    async def run_lifecycle(self) -> None:
        await self.provider.run_lifecycle()


class TestLocalDiskObjectStore(ObjectStoreConformance):
    @pytest.fixture
    def harness(self, tmp_path: Path) -> Harness:
        return Harness(tmp_path)
