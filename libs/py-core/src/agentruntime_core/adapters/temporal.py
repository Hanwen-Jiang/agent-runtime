"""DurableEngine 的 Temporal 适配器。语义见 agentruntime_core.providers.durable_engine。

开发与生产用同一份代码，只是连的 Temporal 服务不同（本地开发服务或自建集群）。
作用域记在工作流的 memo 里（agentruntime_scope），每次访问先 describe 核对；不一致一律按不存在处理。
Go 侧见 libs/go-core/adapters/temporal，memo 键与取值相同，两种语言启动的工作流可以互相访问。
"""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from typing import Any, TypeVar

from google.protobuf.message import Message
from temporalio.client import (
    Client,
    WorkflowExecutionDescription,
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowHandle,
)
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import (
    ApplicationError,
    CancelledError,
    TerminatedError,
    TimeoutError as WorkflowTimeoutError,
    WorkflowAlreadyStartedError,
)
from temporalio.service import RPCError, RPCStatusCode

from agentruntime_core.dev._common import scope_id
from agentruntime_core.errors import (
    ConflictError,
    DeniedError,
    InvalidError,
    NotFoundError,
    ProviderError,
    TransientError,
)
from agentruntime_core.providers.durable_engine import (
    WorkflowDescription,
    WorkflowOutcome,
    WorkflowRun,
    WorkflowStatus,
)
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope

M = TypeVar("M", bound=Message)

SCOPE_MEMO_KEY = "agentruntime_scope"
_WORKFLOW_ID = re.compile(r"[^\x00-\x1f\x7f]{1,255}")

_STATUS = {
    WorkflowExecutionStatus.RUNNING: WorkflowStatus.RUNNING,
    WorkflowExecutionStatus.COMPLETED: WorkflowStatus.COMPLETED,
    WorkflowExecutionStatus.FAILED: WorkflowStatus.FAILED,
    WorkflowExecutionStatus.CANCELED: WorkflowStatus.CANCELLED,
    WorkflowExecutionStatus.TERMINATED: WorkflowStatus.TERMINATED,
    WorkflowExecutionStatus.CONTINUED_AS_NEW: WorkflowStatus.CONTINUED_AS_NEW,
    WorkflowExecutionStatus.TIMED_OUT: WorkflowStatus.TIMED_OUT,
}


def map_rpc_error(error: RPCError) -> ProviderError:
    status = error.status
    if status == RPCStatusCode.NOT_FOUND:
        return NotFoundError("not_found", "工作流不存在")
    if status in (RPCStatusCode.INVALID_ARGUMENT, RPCStatusCode.FAILED_PRECONDITION):
        return InvalidError("invalid_argument", error.message[:200])
    if status == RPCStatusCode.ALREADY_EXISTS:
        return ConflictError("already_exists", "工作流已存在")
    if status in (RPCStatusCode.PERMISSION_DENIED, RPCStatusCode.UNAUTHENTICATED):
        return DeniedError("permission_denied", "没有权限访问 Temporal")
    return TransientError("unavailable", "Temporal 暂时不可用")


def _check_id(workflow_id: str) -> None:
    if not isinstance(workflow_id, str) or not _WORKFLOW_ID.fullmatch(workflow_id):
        raise InvalidError("invalid_workflow_id", "工作流 ID 必须是 1 到 255 个可打印字符")


class TemporalDurableEngineProvider:
    def __init__(self, client: Client) -> None:
        self._client = client

    def bind(self, tenant: TenantContext) -> TemporalDurableEngine:
        return TemporalDurableEngine(self._client, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> TemporalDurableEngine:
        return TemporalDurableEngine(self._client, platform_scope(purpose))

    async def aclose(self) -> None:
        return None


class TemporalDurableEngine:
    def __init__(self, client: Client, scope: Scope) -> None:
        self._client = client
        self._owner = scope_id(scope)

    async def _describe(self, workflow_id: str) -> tuple[WorkflowHandle[Any, Any], WorkflowExecutionDescription] | None:
        """返回句柄与描述；工作流不存在时返回 None。不核对作用域。"""
        handle = self._client.get_workflow_handle(workflow_id)
        try:
            return handle, await handle.describe()
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                return None
            raise map_rpc_error(error) from error

    async def _owned(self, workflow_id: str) -> tuple[WorkflowHandle[Any, Any], WorkflowExecutionDescription]:
        _check_id(workflow_id)
        found = await self._describe(workflow_id)
        if found is None or await found[1].memo_value(SCOPE_MEMO_KEY, default=None) != self._owner:
            raise NotFoundError("not_found", "工作流不存在")
        return found

    async def _existing_run(self, workflow_type: str, workflow_id: str) -> WorkflowRun | None:
        found = await self._describe(workflow_id)
        if found is None:
            return None
        _, description = found
        if await description.memo_value(SCOPE_MEMO_KEY, default=None) != self._owner:
            raise ConflictError("id_in_use", "工作流 ID 已被别的作用域使用")
        if description.status != WorkflowExecutionStatus.RUNNING:
            return None
        if description.workflow_type != workflow_type:
            raise ConflictError("type_mismatch", "同一 ID 的工作流类型不同")
        return WorkflowRun(workflow_id=workflow_id, run_id=description.run_id, already_started=True)

    async def start_workflow(
        self,
        workflow_type: str,
        *,
        workflow_id: str,
        task_queue: str,
        input: Message | None = None,
        execution_timeout: timedelta | None = None,
    ) -> WorkflowRun:
        _check_id(workflow_id)
        existing = await self._existing_run(workflow_type, workflow_id)
        if existing is not None:
            return existing
        try:
            handle = await self._client.start_workflow(
                workflow_type,
                args=[input] if input is not None else [],
                id=workflow_id,
                task_queue=task_queue,
                execution_timeout=execution_timeout,
                memo={SCOPE_MEMO_KEY: self._owner},
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )
        except WorkflowAlreadyStartedError:
            existing = await self._existing_run(workflow_type, workflow_id)
            if existing is None:
                raise ConflictError("id_in_use", "工作流 ID 冲突") from None
            return existing
        except RPCError as error:
            raise map_rpc_error(error) from error
        return WorkflowRun(workflow_id=workflow_id, run_id=handle.first_execution_run_id or handle.result_run_id or "", already_started=False)

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
        _check_id(workflow_id)
        existing = await self._existing_run(workflow_type, workflow_id)
        try:
            handle = await self._client.start_workflow(
                workflow_type,
                args=[input] if input is not None else [],
                id=workflow_id,
                task_queue=task_queue,
                memo={SCOPE_MEMO_KEY: self._owner},
                start_signal=signal,
                start_signal_args=[payload] if payload is not None else [],
            )
        except RPCError as error:
            raise map_rpc_error(error) from error
        if existing is not None:
            return existing
        return WorkflowRun(workflow_id=workflow_id, run_id=handle.result_run_id or "", already_started=False)

    async def signal(self, workflow_id: str, signal: str, payload: Message | None = None) -> None:
        handle, _ = await self._owned(workflow_id)
        try:
            if payload is None:
                await handle.signal(signal)
            else:
                await handle.signal(signal, payload)
        except RPCError as error:
            raise map_rpc_error(error) from error

    async def query(self, workflow_id: str, query: str, result_type: type[M]) -> M:
        handle, _ = await self._owned(workflow_id)
        try:
            return await handle.query(query, result_type=result_type)
        except RPCError as error:
            raise map_rpc_error(error) from error

    async def cancel(self, workflow_id: str, *, reason: str = "") -> None:
        handle, description = await self._owned(workflow_id)
        if description.status != WorkflowExecutionStatus.RUNNING:
            return
        try:
            await handle.cancel()
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise map_rpc_error(error) from error

    async def describe(self, workflow_id: str) -> WorkflowDescription:
        _, d = await self._owned(workflow_id)
        status = _STATUS.get(d.status) if d.status is not None else None
        return WorkflowDescription(
            workflow_id=d.id,
            run_id=d.run_id,
            workflow_type=d.workflow_type,
            task_queue=d.task_queue,
            status=status or WorkflowStatus.RUNNING,
            start_time=d.start_time,
            close_time=d.close_time,
        )

    async def result(self, workflow_id: str, result_type: type[M], *, timeout: timedelta | None = None) -> WorkflowOutcome[M]:
        await self._owned(workflow_id)
        # 结果类型只能在创建句柄时指定。
        handle = self._client.get_workflow_handle(workflow_id, result_type=result_type)
        try:
            value = await asyncio.wait_for(handle.result(), timeout=timeout.total_seconds() if timeout else None)
        except TimeoutError as error:
            raise TransientError("timeout", "等待工作流结束超时") from error
        except WorkflowFailureError as error:
            cause = error.cause
            if isinstance(cause, CancelledError):
                return WorkflowOutcome(status=WorkflowStatus.CANCELLED, value=None, failure=str(cause))
            if isinstance(cause, TerminatedError):
                return WorkflowOutcome(status=WorkflowStatus.TERMINATED, value=None, failure=str(cause))
            if isinstance(cause, WorkflowTimeoutError):
                return WorkflowOutcome(status=WorkflowStatus.TIMED_OUT, value=None, failure=str(cause))
            message = cause.message if isinstance(cause, ApplicationError) else str(cause)
            return WorkflowOutcome(status=WorkflowStatus.FAILED, value=None, failure=message)
        except RPCError as error:
            raise map_rpc_error(error) from error
        return WorkflowOutcome(status=WorkflowStatus.COMPLETED, value=value, failure="")
