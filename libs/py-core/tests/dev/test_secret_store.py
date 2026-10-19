"""开发实现：本地文件 SecretStore。"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from agentruntime_core.conformance.secret_store import SecretStoreConformance
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.testing import FakeClock, FaultInjector


class Harness:
    max_ttl = timedelta(hours=1)

    def __init__(self, path: Path) -> None:
        self.clock = FakeClock()
        self.faults = FaultInjector()
        self.provider = LocalFileSecretStoreProvider(path, max_ttl=self.max_ttl, clock=self.clock, faults=self.faults)

    async def advance(self, delta: timedelta) -> None:
        self.clock.advance(delta)
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)

    def seed_secret(self, tenant_id: str | None, name: str, value: str) -> None:
        self.provider.seed_secret(tenant_id, name, value)

    def grant_role(self, tenant_id: str | None, role: str) -> None:
        self.provider.grant_role(tenant_id, role)


class TestLocalFileSecretStore(SecretStoreConformance):
    @pytest.fixture
    def harness(self, tmp_path: Path) -> Harness:
        return Harness(tmp_path / "secrets.json")
