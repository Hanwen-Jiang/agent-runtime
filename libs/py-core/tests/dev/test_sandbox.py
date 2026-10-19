"""开发实现：本地子进程 Sandbox（仅限开发环境）。"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from agentruntime_core.conformance.sandbox import SandboxConformance
from agentruntime_core.dev.sandbox import LocalSubprocessSandboxProvider
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.testing import FaultInjector


class Harness:
    def __init__(self, root: Path) -> None:
        self.faults = FaultInjector()
        self.provider = LocalSubprocessSandboxProvider(root, faults=self.faults)

    async def advance(self, delta: timedelta) -> None:
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)


class TestLocalSubprocessSandbox(SandboxConformance):
    @pytest.fixture
    async def harness(self, tmp_path: Path):
        harness = Harness(tmp_path)
        yield harness
        await harness.provider.aclose()
