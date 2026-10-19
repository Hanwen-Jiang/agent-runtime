"""命令行 HumanChannel（开发实现）。语义见 agentruntime_core.providers.human_channel。

问题打印到 output；人用 handle_line 输入一行回复（格式：<租户或 _platform> <问题 ID> <选项> [指导意见]），
测试直接调用 receive_answer。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TextIO

from agentruntime_core.clock import Clock, SystemClock
from agentruntime_core.dev._common import PLATFORM_ID, scope_id
from agentruntime_core.errors import ConflictError, InvalidError, NotFoundError
from agentruntime_core.providers.human_channel import Question, QuestionStatus, Reply, ReplySink
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector


@dataclass
class _Entry:
    question: Question
    status: QuestionStatus = QuestionStatus.PENDING


class CliHumanChannelProvider:
    def __init__(
        self,
        sink: ReplySink,
        *,
        output: TextIO | None = None,
        clock: Clock | None = None,
        faults: FaultInjector | None = None,
    ) -> None:
        self._sink = sink
        self._output = output if output is not None else sys.stdout
        self._clock = clock or SystemClock()
        self._faults = faults or FaultInjector()
        # 作用域 → 问题 ID → 问题
        self._questions: dict[str, dict[str, _Entry]] = {}

    def bind(self, tenant: TenantContext) -> CliHumanChannel:
        return CliHumanChannel(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> CliHumanChannel:
        return CliHumanChannel(self, platform_scope(purpose))

    async def aclose(self) -> None:
        return None

    def _entry(self, owner: str, question_id: str) -> _Entry | None:
        entry = self._questions.get(owner, {}).get(question_id)
        if entry is not None and entry.status is QuestionStatus.PENDING and self._clock.now() >= entry.question.deadline:
            entry.status = QuestionStatus.EXPIRED
        return entry

    # --- 开发专用：接收人的回复 ---

    async def receive_answer(
        self, tenant_id: str | None, question_id: str, option: str, *, guidance: str = "", user_id: str = ""
    ) -> bool:
        owner = tenant_id or PLATFORM_ID
        entry = self._entry(owner, question_id)
        if entry is None or entry.status is not QuestionStatus.PENDING or option not in entry.question.options:
            return False
        entry.status = QuestionStatus.ANSWERED
        await self._sink.deliver(
            Reply(
                question_id=question_id,
                task_id=entry.question.task_id,
                tenant_id="" if owner == PLATFORM_ID else owner,
                option=option,
                guidance=guidance,
                replied_by_user_id=user_id,
                replied_at=self._clock.now(),
            )
        )
        return True

    async def handle_line(self, line: str, *, user_id: str = "") -> bool:
        parts = line.strip().split(maxsplit=3)
        if len(parts) < 3:
            return False
        owner, question_id, option = parts[:3]
        guidance = parts[3] if len(parts) == 4 else ""
        return await self.receive_answer(None if owner == PLATFORM_ID else owner, question_id, option, guidance=guidance, user_id=user_id)


class CliHumanChannel:
    def __init__(self, provider: CliHumanChannelProvider, scope: Scope) -> None:
        self._provider = provider
        self._owner = scope_id(scope)

    async def ask(self, question: Question) -> None:
        self._provider._faults.check("ask")
        if not question.question_id or not question.task_id or not question.text:
            raise InvalidError("invalid_question", "问题必须有 question_id、task_id 与正文")
        if not question.options or len(set(question.options)) != len(question.options):
            raise InvalidError("invalid_question", "选项至少一个且不得重复")
        if question.deadline <= self._provider._clock.now():
            raise InvalidError("invalid_question", "截止时间必须晚于现在")
        questions = self._provider._questions.setdefault(self._owner, {})
        existing = questions.get(question.question_id)
        if existing is not None:
            if existing.question != question:
                raise ConflictError("question_conflict", "同一 question_id 的内容不同")
            return
        questions[question.question_id] = _Entry(question=question)
        options = " / ".join(question.options)
        self._provider._output.write(
            f"[{self._owner} {question.question_id}] {question.text}\n  选项：{options}\n  截止：{question.deadline.isoformat()}\n"
        )

    async def cancel(self, question_id: str) -> None:
        self._provider._faults.check("cancel")
        entry = self._provider._entry(self._owner, question_id)
        if entry is None:
            raise NotFoundError("not_found", "问题不存在")
        if entry.status is QuestionStatus.PENDING:
            entry.status = QuestionStatus.CANCELLED

    async def status(self, question_id: str) -> QuestionStatus:
        self._provider._faults.check("status")
        entry = self._provider._entry(self._owner, question_id)
        if entry is None:
            raise NotFoundError("not_found", "问题不存在")
        return entry.status
