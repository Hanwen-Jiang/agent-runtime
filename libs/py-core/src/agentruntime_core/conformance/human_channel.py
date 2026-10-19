"""HumanChannel 的一致性测试。语义见 agentruntime_core.providers.human_channel。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Protocol

import pytest

from agentruntime_core.conformance.common import TENANT_A_ID, TENANT_B_ID, Harness, StandardRules, expect_error, tenant_a, tenant_b
from agentruntime_core.errors import ErrorCategory, NotFoundError
from agentruntime_core.providers.human_channel import HumanChannel, HumanChannelProvider, Question, QuestionStatus, Reply

ESCALATE_OPTIONS = ("continue_as_is", "replan_with_guidance", "stop_and_wrap_up")


class HumanChannelHarness(Harness[HumanChannelProvider], Protocol):
    def now(self) -> datetime:
        """实现所用时钟的当前时间，用来设截止时间。"""
        ...

    async def answer(
        self, tenant_id: str | None, question_id: str, option: str, *, guidance: str = "", user_id: str = "u-alice"
    ) -> bool:
        """模拟人在通道上回复；返回通道是否接受。tenant_id 为 None 表示平台作用域的问题。"""
        ...

    @property
    def delivered(self) -> list[Reply]:
        """ReplySink 收到的全部回复。"""
        ...


class HumanChannelConformance(StandardRules):
    probe_operation = "ask"

    def _question(self, harness: HumanChannelHarness, question_id: str = "q-1", **overrides: object) -> Question:
        base = Question(
            question_id=question_id,
            task_id="tsk_01JA8Q3V5K7M9P2R4T6W8Y0Z1B",
            text="华东退款分析要不要覆盖退货运费？",
            options=ESCALATE_OPTIONS,
            deadline=harness.now() + timedelta(hours=1),
            state_summary="已完成取数与归因",
        )
        return replace(base, **overrides)  # type: ignore[arg-type]

    async def write_probe(self, client: HumanChannel, name: str) -> None:
        await client.ask(self._question(self.__dict__["_harness"], name))

    async def probe_visible(self, client: HumanChannel, name: str) -> bool:
        try:
            await client.status(name)
        except NotFoundError:
            return False
        return True

    @pytest.fixture(autouse=True)
    def _remember_harness(self, harness: HumanChannelHarness) -> None:
        self.__dict__["_harness"] = harness

    @pytest.fixture
    def channel(self, harness: HumanChannelHarness) -> HumanChannel:
        return harness.provider.bind(tenant_a())

    async def test_ask_then_pending(self, harness: HumanChannelHarness, channel: HumanChannel) -> None:
        await channel.ask(self._question(harness))
        assert await channel.status("q-1") is QuestionStatus.PENDING
        assert harness.delivered == []

    async def test_valid_answer_is_delivered_once(self, harness: HumanChannelHarness, channel: HumanChannel) -> None:
        question = self._question(harness)
        await channel.ask(question)
        assert await harness.answer(TENANT_A_ID, "q-1", "replan_with_guidance", guidance="只看华东") is True
        assert await harness.answer(TENANT_A_ID, "q-1", "continue_as_is") is False
        assert await channel.status("q-1") is QuestionStatus.ANSWERED
        [reply] = harness.delivered
        assert reply.question_id == "q-1" and reply.option == "replan_with_guidance" and reply.guidance == "只看华东"
        assert reply.tenant_id == TENANT_A_ID and reply.task_id == question.task_id
        assert reply.replied_by_user_id == "u-alice"

    async def test_answer_outside_options_is_rejected(self, harness: HumanChannelHarness, channel: HumanChannel) -> None:
        await channel.ask(self._question(harness))
        assert await harness.answer(TENANT_A_ID, "q-1", "随你决定") is False
        assert harness.delivered == []
        assert await channel.status("q-1") is QuestionStatus.PENDING

    async def test_question_expires_at_deadline(self, harness: HumanChannelHarness, channel: HumanChannel) -> None:
        await channel.ask(self._question(harness, deadline=harness.now() + timedelta(minutes=10)))
        await harness.advance(timedelta(minutes=11))
        assert await channel.status("q-1") is QuestionStatus.EXPIRED
        assert await harness.answer(TENANT_A_ID, "q-1", "continue_as_is") is False
        assert harness.delivered == []

    async def test_cancelled_question_takes_no_answer(self, harness: HumanChannelHarness, channel: HumanChannel) -> None:
        await channel.ask(self._question(harness))
        await channel.cancel("q-1")
        await channel.cancel("q-1")
        assert await channel.status("q-1") is QuestionStatus.CANCELLED
        assert await harness.answer(TENANT_A_ID, "q-1", "continue_as_is") is False
        assert harness.delivered == []

    async def test_ask_is_idempotent_by_question_id(self, harness: HumanChannelHarness, channel: HumanChannel) -> None:
        question = self._question(harness)
        await channel.ask(question)
        await channel.ask(question)
        with expect_error(ErrorCategory.CONFLICT):
            await channel.ask(replace(question, text="另一个问题"))

    @pytest.mark.parametrize("case", ["no_options", "duplicate_options", "past_deadline"])
    async def test_malformed_question_is_invalid(self, harness: HumanChannelHarness, channel: HumanChannel, case: str) -> None:
        overrides = {
            "no_options": {"options": ()},
            "duplicate_options": {"options": ("yes", "yes")},
            "past_deadline": {"deadline": harness.now() - timedelta(seconds=1)},
        }[case]
        with expect_error(ErrorCategory.INVALID):
            await channel.ask(self._question(harness, **overrides))

    async def test_other_tenant_cannot_see_cancel_or_answer(self, harness: HumanChannelHarness, channel: HumanChannel) -> None:
        await channel.ask(self._question(harness))
        b = harness.provider.bind(tenant_b())
        with expect_error(ErrorCategory.NOT_FOUND):
            await b.status("q-1")
        with expect_error(ErrorCategory.NOT_FOUND):
            await b.cancel("q-1")
        assert await harness.answer(TENANT_B_ID, "q-1", "stop_and_wrap_up", user_id="u-bob") is False
        assert harness.delivered == []
        assert await channel.status("q-1") is QuestionStatus.PENDING

    async def test_unknown_question_is_not_found(self, channel: HumanChannel) -> None:
        with expect_error(ErrorCategory.NOT_FOUND):
            await channel.status("q-missing")
        with expect_error(ErrorCategory.NOT_FOUND):
            await channel.cancel("q-missing")
