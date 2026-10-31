"""立刻打断（docs/plan/running-task-interaction.md 第 2 节）：放弃正在进行的调用，马上生效。

- 能被打断的调用放在 Scope 里跑（run）。打断时 Scope 取消它；取消整个 Task 也表现为取消，靠 Task.cancelling() 区分：调用方这个
  任务自己被要求取消时是取消整个 Task（或它所在的 Node 被取消），优先，原样抛出；只有 Scope 被打断时才抛 Interrupted。
- 工具被打断后的撤销结果（restored、failed、no_checkpoint、not_needed）由 activity 报告。Python SDK 对“请求取消后以 CancelledError
  结束”的 activity 一律上报不带 details 的取消，结果带不回来，所以 activity 抛 ApplicationError（type tool_interrupted，details
  第一项是结果）。工作流拿到它要当成取消处理（is_tool_interrupted）。

工作流代码必须确定：这里只用工作流的任务与取消。tool_interrupted、undo_of、interruption_note 是纯函数，activity 也用。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, TypeVar

from temporalio.exceptions import ApplicationError, FailureError

from agentruntime_worker.workflows.failures import caused_by_cancel

T = TypeVar("T")

TOOL_INTERRUPTED = "tool_interrupted"
RESTORED = "restored"
UNDO_FAILED = "failed"
NO_CHECKPOINT = "no_checkpoint"
NOT_NEEDED = "not_needed"
NO_RESULT_NOTE = "被用户打断，没有结果"
_NOTES = {
    RESTORED: "被用户打断，已撤销这次调用的改动，工作区的文件回到了调用之前。网络请求与已经跑完的后台效果撤不回。",
    UNDO_FAILED: "被用户打断，没能撤销这次调用的改动，工作区里可能留着改到一半的文件，先查看再继续。",
    NO_CHECKPOINT: "被用户打断，没能撤销这次调用的改动（没有检查点：不在 git 工作副本里，或者不是对工作副本的改动），先查看再继续。",
    NOT_NEEDED: NO_RESULT_NOTE,
}


def tool_interrupted(undo: str) -> ApplicationError:
    """工具调用被打断之后，activity 用它结束，带回撤销的结果。"""
    return ApplicationError("tool call interrupted", undo, type=TOOL_INTERRUPTED, non_retryable=True)


def _application(err: BaseException | None) -> ApplicationError | None:
    cause = err
    while cause is not None:
        if isinstance(cause, ApplicationError) and cause.type == TOOL_INTERRUPTED:
            return cause
        cause = cause.cause if isinstance(cause, FailureError) else None
    return None


def is_tool_interrupted(err: BaseException | None) -> bool:
    return _application(err) is not None


def undo_of(err: BaseException | None) -> str:
    """撤销的结果；取不到（是别的取消）时是 not_needed。"""
    found = _application(err)
    details = found.details if found is not None else ()
    return str(details[0]) if details else NOT_NEEDED


def interruption_note(err: BaseException | None) -> str:
    """告诉模型与用户发生了什么：照实说，取不到结果时不说撤销了。"""
    return _NOTES.get(undo_of(err), NO_RESULT_NOTE)


class Interrupted(Exception):
    """调用被立刻打断。cause 是工具报告的失败（有的话），interruption_note 据此说明。"""

    def __init__(self, cause: BaseException | None = None) -> None:
        super().__init__(NO_RESULT_NOTE)
        self.reported = cause


@dataclass
class Scope:
    """一次能被打断的调用。"""

    task: asyncio.Future[Any]
    interrupted: bool = False

    def interrupt(self) -> None:
        self.interrupted = True
        self.task.cancel()


def asked_to_cancel() -> bool:
    """这个任务被要求取消过（包括取消被 activity 的正常结束吞掉的情况）。"""
    current = asyncio.current_task()
    return current is not None and current.cancelling() > 0


async def run(scopes: list[Scope], call: Awaitable[T]) -> T:
    """跑一次能被打断的调用。被打断时抛 Interrupted；取消整个 Task 时照常抛取消。"""
    scope = Scope(asyncio.ensure_future(call))
    scopes.append(scope)
    try:
        return await scope.task
    except asyncio.CancelledError as err:
        if scope.interrupted and not asked_to_cancel():
            raise Interrupted() from err
        raise
    except FailureError as err:
        if scope.interrupted and (caused_by_cancel(err) or is_tool_interrupted(err)):
            if asked_to_cancel():
                raise asyncio.CancelledError() from err
            raise Interrupted(err) from err
        raise
    finally:
        scopes.remove(scope)
