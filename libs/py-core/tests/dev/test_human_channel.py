"""开发实现：命令行 HumanChannel。"""

from __future__ import annotations

import asyncio
import io
from datetime import datetime, timedelta

import pytest

from agentruntime_core.conformance.human_channel import HumanChannelConformance
from agentruntime_core.dev.human_channel import CliHumanChannelProvider
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.providers.human_channel import Reply
from agentruntime_core.testing import FakeClock, FaultInjector


class RecordingSink:
    def __init__(self) -> None:
        self.replies: list[Reply] = []

    async def deliver(self, reply: Reply) -> None:
        self.replies.append(reply)


class Harness:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.faults = FaultInjector()
        self.sink = RecordingSink()
        self.output = io.StringIO()
        self.provider = CliHumanChannelProvider(self.sink, output=self.output, clock=self.clock, faults=self.faults)

    def now(self) -> datetime:
        return self.clock.now()

    async def advance(self, delta: timedelta) -> None:
        self.clock.advance(delta)
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)

    async def answer(
        self, tenant_id: str | None, question_id: str, option: str, *, guidance: str = "", user_id: str = "u-alice"
    ) -> bool:
        return await self.provider.receive_answer(tenant_id, question_id, option, guidance=guidance, user_id=user_id)

    @property
    def delivered(self) -> list[Reply]:
        return self.sink.replies


class TestCliHumanChannel(HumanChannelConformance):
    @pytest.fixture
    def harness(self) -> Harness:
        return Harness()
