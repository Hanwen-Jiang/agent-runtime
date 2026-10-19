"""HumanChannel（设计书 4.11）：发出问题（问题、选项、截止时间、状态摘要），回复以信号送回 Task 工作流。

只在 Python 侧（human 节点与 Reviewer 问人）使用。本期只定义接口；开发实现是命令行。

回复的去向由构造工厂时传入的 ReplySink 决定：生产中它向 Task 工作流发送 human_reply 信号（附录 K.1），
等待与超时由工作流的信号与持久化定时器处理，通道本身不占线程等待。

语义：
- 选项至少一个、不重复；截止时间必须晚于现在。否则 invalid。
- ask 按 question_id 幂等：同一 id、同样内容重复提交不报错；内容不同：conflict。
- 回复必须是问题给出的选项之一，否则不予接受，也不投递。
- 每个问题最多投递一次回复：已回答、已取消或已过截止时间的问题不再接受回复。
- 投递的 Reply 中，tenant_id 与 task_id 取自问题本身，回复人无法改变。
- status：PENDING、ANSWERED、CANCELLED 或 EXPIRED。问题不存在或属于别的作用域：not_found。cancel 重复调用不报错。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from agentruntime_core.providers.base import Bindable


@dataclass(frozen=True)
class Question:
    question_id: str
    task_id: str
    text: str
    options: tuple[str, ...]
    deadline: datetime
    state_summary: str = ""


class QuestionStatus(enum.StrEnum):
    PENDING = "pending"
    ANSWERED = "answered"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Reply:
    question_id: str
    task_id: str
    tenant_id: str
    option: str
    guidance: str
    replied_by_user_id: str
    replied_at: datetime


class ReplySink(Protocol):
    async def deliver(self, reply: Reply) -> None: ...


class HumanChannel(Protocol):
    async def ask(self, question: Question) -> None: ...

    async def cancel(self, question_id: str) -> None: ...

    async def status(self, question_id: str) -> QuestionStatus: ...


class HumanChannelProvider(Bindable[HumanChannel], Protocol):
    pass
