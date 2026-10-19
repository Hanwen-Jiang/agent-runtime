"""开发实现：本地函数 ToolDriver。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from agentruntime_core.conformance.tool_driver import ToolDriverConformance
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.providers.tool_driver import ToolHandler, ToolSpec
from agentruntime_core.testing import FaultInjector


class Harness:
    def __init__(self) -> None:
        self.faults = FaultInjector()
        self.provider = LocalToolDriverProvider(faults=self.faults)

    async def advance(self, delta: timedelta) -> None:
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)

    def register(self, spec: ToolSpec, handler: ToolHandler, *, tenant_id: str | None = None) -> None:
        self.provider.register(spec, handler, tenant_id=tenant_id)

    def grant(self, tenant_id: str | None, permission: str) -> None:
        self.provider.grant(tenant_id, permission)


class TestLocalToolDriver(ToolDriverConformance):
    @pytest.fixture
    def harness(self) -> Harness:
        return Harness()
