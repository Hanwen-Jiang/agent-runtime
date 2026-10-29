"""工作流里重试决策调用（TMP 计划 eval-observability-and-runtime-fixes 第 4 节；DECISION_RETRY_PATCH 之后的运行）。

plan_round、review_round、tool_loop_step 在服务端只试一次，由这里按 timeouts.decision_backoff 重试，最多
DECISION_MAX_ATTEMPTS 次；第 2 次起每次尝试之前推 decision_retry（有 EventPublisher 时），客户端看得到“规划调用超时，
正在重试（2/5）”。之前由服务端重试，Python 的 activity 推不了进度事件，界面上一直停在“规划下一轮”。

不可重试的错误（non_retryable）与取消原样抛出。一步决策连同重试的总时限是 DECISION_TOTAL：到了、或剩下的不够
DECISION_MIN_ATTEMPT 就不再试，抛 DecisionExhausted，由调用方退回不调模型的做法（规划、评审）或让节点失败（tool_loop 的一步）。

是工作流代码：时间用 workflow.now()，等待是工作流的定时器。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, FailureError, TimeoutError

with workflow.unsafe.imports_passed_through():
    from agentruntime_worker.workflows import timeouts
    from agentruntime_worker.workflows.events import EventPublisher, message_code
    from agentruntime_worker.workflows.failures import caused_by_cancel, failure_message

_SNAKE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class DecisionExhausted(Exception):
    """重试用完或到了总时限；last 是最后一次失败。"""

    def __init__(self, last: FailureError, attempts: int) -> None:
        super().__init__(failure_message(last))
        self.last = last
        self.attempts = attempts


def error_code(err: BaseException | None) -> str:
    """一次失败的代码：超时记 activity_timeout；否则取 ApplicationError 的类型（已经是 snake_case 时），或说明开头的
    “<代码>: ”（ProviderError 的写法，例如网关的 models_exhausted），再不行把类型名整理成 snake_case。取不到时为空。"""
    cause = err
    while cause is not None:
        if isinstance(cause, TimeoutError):
            return "activity_timeout"
        if isinstance(cause, ApplicationError):
            kind = cause.type or ""
            if _SNAKE.match(kind):
                return kind
            head = str(cause.message or "").split(":", 1)[0].strip()
            if _SNAKE.match(head):
                return head
            return message_code(kind)
        cause = cause.cause if isinstance(cause, FailureError) else None
    return ""


def _retryable(err: FailureError) -> bool:
    cause = err.cause if isinstance(err, ActivityError) else err
    return not (isinstance(cause, ApplicationError) and cause.non_retryable)


async def run(
    activity: str,
    arg: Any,
    *,
    task_queue: str,
    result_type: type,
    step: str,
    round_seq: int,
    events: EventPublisher | None,
    node_id: str = "",
    about: str = "",
) -> Any:
    """执行一个决策 activity，失败时在工作流里重试（规则见模块说明）。about 区分同一 Round 同一步的不同调用（tool_loop 的 Node 与步）。"""
    started = workflow.now()
    last: FailureError | None = None
    attempt = 0
    for attempt in range(1, timeouts.DECISION_MAX_ATTEMPTS + 1):
        left = timeouts.DECISION_TOTAL - (workflow.now() - started)
        if attempt > 1 and events is not None:
            await events.decision_retry(
                round_seq, step, attempt, timeouts.DECISION_MAX_ATTEMPTS,
                node_id=node_id, error_code=error_code(last), error=failure_message(last) if last is not None else "", about=about,
            )
        try:
            return await workflow.execute_activity(
                activity,
                arg,
                task_queue=task_queue,
                start_to_close_timeout=min(timeouts.MODEL_DECISION_START_TO_CLOSE, left),
                retry_policy=RetryPolicy(maximum_attempts=1),
                result_type=result_type,
            )
        except FailureError as err:
            if caused_by_cancel(err):
                raise asyncio.CancelledError() from err
            if not _retryable(err):
                raise
            last = err
        if attempt == timeouts.DECISION_MAX_ATTEMPTS:
            break
        wait = timeouts.decision_backoff(attempt)
        if timeouts.DECISION_TOTAL - (workflow.now() - started) - wait < timeouts.DECISION_MIN_ATTEMPT:
            break
        await asyncio.sleep(wait.total_seconds())
    assert last is not None
    raise DecisionExhausted(last, attempt)
