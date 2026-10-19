"""工作流里对失败的判断：取失败说明、判断是不是取消、映射到附录 D.6 的失败分类。纯函数。"""

from __future__ import annotations

from temporalio.exceptions import ApplicationError, CancelledError, FailureError, TimeoutError

FAILURE_CLASSES = ("transient", "invalid_output", "denied", "upstream_failed")


def failure_message(err: BaseException) -> str:
    """取最内层的失败说明，例如 activity 里抛出的 ApplicationError。"""
    message = str(err)
    cause: BaseException | None = err
    while isinstance(cause, FailureError):
        if cause.message:
            message = cause.message
        cause = cause.cause
    return message


def caused_by_cancel(err: BaseException) -> bool:
    cause: BaseException | None = err
    while cause is not None:
        if isinstance(cause, CancelledError):
            return True
        cause = cause.cause if isinstance(cause, FailureError) else None
    return False


def failure_class(err: BaseException) -> str:
    """附录 D.6 的失败分类：ApplicationError 的 type 是分类名时取它，超时算 transient，其余不分类（空串）。"""
    cause: BaseException | None = err
    while cause is not None:
        if isinstance(cause, ApplicationError) and cause.type in FAILURE_CLASSES:
            return cause.type
        if isinstance(cause, TimeoutError):
            return "transient"
        cause = cause.cause if isinstance(cause, FailureError) else None
    return ""
