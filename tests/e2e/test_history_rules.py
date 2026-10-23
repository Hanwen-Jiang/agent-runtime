"""事件历史检查（e2e_support.history）第 1 条的例外：只放行被服务端拒绝、随后重试成功并正常结束的 UNHANDLED_COMMAND。

工作流要结束（完成或 Continue-As-New）时恰好来了信号，服务端以 UNHANDLED_COMMAND 拒绝这次结束命令，工作流先处理
信号再重试；这是 Temporal 的正常行为（夜间任务里 Session 工作流收到重复 task_finished 时出现过）。其余
WorkflowTaskFailed 照旧算异常。用造出来的事件历史检查，不需要端到端环境。
"""

from __future__ import annotations

from typing import Any

import pytest
from temporalio.api.common.v1 import WorkflowType
from temporalio.api.enums.v1 import EventType, WorkflowTaskFailedCause
from temporalio.api.failure.v1 import Failure
from temporalio.api.history.v1 import (
    HistoryEvent,
    WorkflowExecutionSignaledEventAttributes,
    WorkflowExecutionStartedEventAttributes,
    WorkflowTaskFailedEventAttributes,
)
from temporalio.client import WorkflowHistory

from e2e_support import history as h

UNHANDLED_COMMAND = WorkflowTaskFailedCause.WORKFLOW_TASK_FAILED_CAUSE_UNHANDLED_COMMAND
NON_DETERMINISTIC = WorkflowTaskFailedCause.WORKFLOW_TASK_FAILED_CAUSE_NON_DETERMINISTIC_ERROR
WORKER_FAILURE = WorkflowTaskFailedCause.WORKFLOW_TASK_FAILED_CAUSE_WORKFLOW_WORKER_UNHANDLED_FAILURE
BAD_ACTIVITY = WorkflowTaskFailedCause.WORKFLOW_TASK_FAILED_CAUSE_BAD_SCHEDULE_ACTIVITY_ATTRIBUTES
UNHANDLED_MESSAGE = "UnhandledCommand"

Step = list[tuple[int, dict[str, Any]]]


def task_completed() -> Step:
    return [
        (EventType.EVENT_TYPE_WORKFLOW_TASK_SCHEDULED, {}),
        (EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED, {}),
        (EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED, {}),
    ]


def task_failed(cause: int, message: str = UNHANDLED_MESSAGE) -> Step:
    attrs = WorkflowTaskFailedEventAttributes(cause=cause, failure=Failure(message=message))
    return [
        (EventType.EVENT_TYPE_WORKFLOW_TASK_SCHEDULED, {}),
        (EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED, {}),
        (EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED, {"workflow_task_failed_event_attributes": attrs}),
    ]


def signaled(name: str = "task_finished") -> Step:
    attrs = WorkflowExecutionSignaledEventAttributes(signal_name=name)
    return [(EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED, {"workflow_execution_signaled_event_attributes": attrs})]


def closed(close: str) -> Step:
    kind = next(name for name, c in h.CLOSE_EVENTS.items() if c == close)
    return [(EventType.Value(f"EVENT_TYPE_{kind}"), {})]


def run(*steps: Step, workflow_type: str = "SessionWorkflow") -> WorkflowHistory:
    started = WorkflowExecutionStartedEventAttributes(workflow_type=WorkflowType(name=workflow_type), original_execution_run_id="run-1")
    flat: Step = [(EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED, {"workflow_execution_started_event_attributes": started})]
    for step in steps:
        flat += step
    events = [HistoryEvent(event_id=i, event_type=event_type, **attrs) for i, (event_type, attrs) in enumerate(flat, start=1)]
    return WorkflowHistory("session/t1/w1", events)


def rejected_continue_as_new(cause: int = UNHANDLED_COMMAND, close: str = "continued_as_new") -> WorkflowHistory:
    """夜间任务里出现过的样子：结束命令被拒，期间到了两个重复的 task_finished，重试后正常结束。"""
    return run(task_completed(), signaled("submit_task"), task_failed(cause), signaled(), signaled(), task_completed(), closed(close))


def still_running() -> WorkflowHistory:
    return run(task_completed(), signaled("submit_task"), task_completed())


def test_rejected_continue_as_new_that_is_retried_passes() -> None:
    h.assert_clean(h.analyze(rejected_continue_as_new()), close="continued_as_new")
    h.assert_session_clean([rejected_continue_as_new(), still_running()])


def test_rejected_completion_that_is_retried_passes() -> None:
    report = h.analyze(rejected_continue_as_new(close="completed"))
    h.assert_clean(report, close="completed")


def test_rejection_is_still_reported_in_the_raw_list() -> None:
    # 原始的失败列表照旧记下每一次 WorkflowTaskFailed（Claim-Check 的用例靠它看 ObjectStore 不可用的报错）。
    report = h.analyze(rejected_continue_as_new())
    assert report.workflow_task_failures == [f"{int(UNHANDLED_COMMAND)}: {UNHANDLED_MESSAGE}"]


@pytest.mark.parametrize("cause", [NON_DETERMINISTIC, WORKER_FAILURE, BAD_ACTIVITY])
def test_other_causes_still_fail_even_when_retried(cause: int) -> None:
    with pytest.raises(AssertionError, match="WorkflowTaskFailed"):
        h.assert_clean(h.analyze(rejected_continue_as_new(cause)), close="continued_as_new")


def test_unhandled_command_in_a_run_that_is_still_open_fails() -> None:
    history = run(task_completed(), signaled("submit_task"), task_failed(UNHANDLED_COMMAND), signaled(), task_completed())
    with pytest.raises(AssertionError, match="WorkflowTaskFailed"):
        h.assert_clean(h.analyze(history), close=None)
    with pytest.raises(AssertionError, match="WorkflowTaskFailed"):
        h.assert_session_clean([rejected_continue_as_new(), history])


@pytest.mark.parametrize("close", ["failed", "terminated", "timed_out"])
def test_unhandled_command_in_a_run_that_did_not_end_normally_fails(close: str) -> None:
    with pytest.raises(AssertionError, match="WorkflowTaskFailed"):
        h.assert_clean(h.analyze(rejected_continue_as_new(close=close)), close=close)


def test_unhandled_command_without_a_later_successful_task_fails() -> None:
    # 拒绝之后没有成功完成的工作流任务，就不算“重试成功”：例如被拒之后直接被终止。
    history = run(task_completed(), task_failed(UNHANDLED_COMMAND), signaled(), closed("terminated"))
    with pytest.raises(AssertionError, match="WorkflowTaskFailed"):
        h.assert_clean(h.analyze(history), close="terminated")


def test_only_the_unexpected_failures_are_reported() -> None:
    history = run(
        task_completed(),
        task_failed(WORKER_FAILURE, "boom"),
        task_completed(),
        task_failed(UNHANDLED_COMMAND),
        signaled(),
        task_completed(),
        closed("continued_as_new"),
    )
    with pytest.raises(AssertionError) as raised:
        h.assert_clean(h.analyze(history), close="continued_as_new")
    message = str(raised.value)
    assert "boom" in message
    assert UNHANDLED_MESSAGE not in message
