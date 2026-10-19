"""开发实现：控制台 Telemetry（同时在内存里保留导出的记录）。"""

from __future__ import annotations

import asyncio
import io
from dataclasses import asdict
from datetime import timedelta

import pytest

from agentruntime_core.conformance.telemetry import Exported, ExportedLog, ExportedMetric, ExportedSpan, TelemetryConformance
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.testing import FakeClock, FaultInjector


class Harness:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.faults = FaultInjector()
        self.stream = io.StringIO()
        self.provider = ConsoleTelemetryProvider(stream=self.stream, clock=self.clock, faults=self.faults)

    async def advance(self, delta: timedelta) -> None:
        self.clock.advance(delta)
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)

    def exported(self) -> Exported:
        # 控制台输出也必须不含明文，所以把输出文本一并放进日志记录里参与检查。
        console = ExportedLog(level="console", message=self.stream.getvalue())
        return Exported(
            spans=tuple(ExportedSpan(**asdict(s)) for s in self.provider.exported_spans),
            metrics=tuple(ExportedMetric(**asdict(m)) for m in self.provider.exported_metrics),
            logs=tuple(ExportedLog(**asdict(entry)) for entry in self.provider.exported_logs) + (console,),
        )


class TestConsoleTelemetry(TelemetryConformance):
    @pytest.fixture
    def harness(self) -> Harness:
        return Harness()
