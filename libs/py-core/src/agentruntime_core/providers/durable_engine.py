"""DurableEngine（设计书 4.2、附录 K）：启动与取消工作流、信号与查询。

两种语言共用：Go 侧见 libs/go-core/providers/durable，方法一一对应，语义一致。

这里只是客户端操作。工作流代码、activity、定时器与版本分支直接用 Temporal SDK 编写（CLAUDE.md 允许工作流代码引用
Temporal），不经过本接口。开发实现是 Temporal 本地开发服务（docs/reference/runtime-quickref.md 第 6 节）。

语义：
- 输入、信号载荷、查询结果与工作流结果都是 protobuf 消息，编码为 json/protobuf（计划 S-01）。
- 工作流 ID：1 到 255 个可打印字符，否则 invalid。实现在工作流上记录所属作用域，每次访问都核对。
- start_workflow 是幂等的：同一 ID 的工作流正在运行时，返回已有的运行，already_started=True，不启动新运行；
  类型不同则报 conflict。ID 属于别的作用域：conflict。
- signal、query、describe、result 访问不存在或属于别的作用域的工作流：not_found。
- cancel 是幂等的：已取消或已结束的工作流再取消不报错；不存在或属于别的作用域：not_found。
- result 等到工作流结束，返回结局；等待超过 timeout：transient。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Generic, Protocol, TypeVar

from google.protobuf.message import Message

from agentruntime_core.providers.base import Bindable

M = TypeVar("M", bound=Message)


class WorkflowStatus(enum.StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TERMINATED = "terminated"
    CONTINUED_AS_NEW = "continued_as_new"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class WorkflowRun:
    workflow_id: str
    run_id: str
    already_started: bool


@dataclass(frozen=True)
class WorkflowDescription:
    workflow_id: str
    run_id: str
    workflow_type: str
    task_queue: str
    status: WorkflowStatus
    start_time: datetime
    close_time: datetime | None


@dataclass(frozen=True)
class WorkflowOutcome(Generic[M]):
    status: WorkflowStatus
    # status = COMPLETED 时的结果。
    value: M | None
    # 失败、取消或超时时的说明，不含凭据。
    failure: str


class DurableEngine(Protocol):
    async def start_workflow(
        self,
        workflow_type: str,
        *,
        workflow_id: str,
        task_queue: str,
        input: Message | None = None,
        execution_timeout: timedelta | None = None,
    ) -> WorkflowRun: ...

    async def signal_with_start(
        self,
        workflow_type: str,
        *,
        workflow_id: str,
        task_queue: str,
        input: Message | None,
        signal: str,
        payload: Message | None,
    ) -> WorkflowRun:
        """工作流不存在就启动，再投递信号（设计书 3.1.1：以 session_id 找到 Session 工作流，不存在就创建）。"""
        ...

    async def signal(self, workflow_id: str, signal: str, payload: Message | None = None) -> None: ...

    async def query(self, workflow_id: str, query: str, result_type: type[M]) -> M: ...

    async def cancel(self, workflow_id: str, *, reason: str = "") -> None: ...

    async def describe(self, workflow_id: str) -> WorkflowDescription: ...

    async def result(
        self, workflow_id: str, result_type: type[M], *, timeout: timedelta | None = None
    ) -> WorkflowOutcome[M]: ...


class DurableEngineProvider(Bindable[DurableEngine], Protocol):
    pass
