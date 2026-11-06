"""Workspace API 的操作工作流（D-06e；做什么见 agentruntime_worker.workspace.operations）。

task-service 的 WorkspaceService 收到请求后起它（workspace 队列，ID wsop-<operation_id>），等它的结果。工作流只排一个
workspace_operation activity（在承载工作区的沙箱里跑 git）：排在工作流自己所在的队列，workspace 池一个池就跑得完
（WS-READ-02）；改之前起在 task 队列的操作照旧排在 program 队列，命令序列不变，旧历史照样回放，不用版本分支。
activity 把非暂时性的错误写进结果；暂时性的
抛出来，按这里的重试策略重试，重试完还不行时这里把它写成 transient 的结果。推送是 write_irreversible，不自动重试
（结果不明时下一次调用先对账）。凭据在输入里是封起来的（wsc1），事件历史里没有明文。工作流代码只排 activity，是确定的。
"""

from __future__ import annotations

import re
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from agentruntime.common.v1 import error_pb2
    from agentruntime.workflow.v1 import workspace_pb2

    from agentruntime_worker import names
    from agentruntime_worker.workflows.failures import failure_message
    from agentruntime_worker.workflows.patches import WORKSPACE_ASYNC_RESULT_PATCH

_CODE = re.compile(r"^([a-z][a-z0-9_]{0,63}): ")


def operation_workflow_id(operation_id: str) -> str:
    return f"wsop-{operation_id}"


@workflow.defn(name=names.WORKSPACE_OPERATION_WORKFLOW)
class WorkspaceOperationWorkflow:
    @workflow.run
    async def run(self, inp: workspace_pb2.WorkspaceOperationInput) -> workspace_pb2.WorkspaceOperationResult:
        enabled = inp.args.fields.get("__async_result")
        persist_async = bool(enabled and enabled.bool_value and workflow.patched(WORKSPACE_ASYNC_RESULT_PATCH))
        push = inp.operation == workspace_pb2.WORKSPACE_OPERATION_PUSH
        retry = RetryPolicy(maximum_attempts=1) if push else RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5))
        queue = names.WORKSPACE_QUEUE if workflow.info().task_queue == names.WORKSPACE_QUEUE else names.PROGRAM_QUEUE
        try:
            result = await workflow.execute_activity(
                names.WORKSPACE_OPERATION,
                inp,
                result_type=workspace_pb2.WorkspaceOperationResult,
                task_queue=queue,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=retry,
            )
        except ActivityError as err:
            message = failure_message(err)
            match = _CODE.match(message)
            code = match.group(1) if match else "workspace_operation_failed"
            text = message[match.end():] if match else "工作区的操作暂时失败，稍后再试"
            result = workspace_pb2.WorkspaceOperationResult(error=error_pb2.Error(category=error_pb2.ERROR_CATEGORY_TRANSIENT, code=code, message=text))


        # Only new async executions add this command. Legacy root histories and
        # already running async workflows replay their original command sequence.
        if persist_async:
            await workflow.execute_activity(
                names.WORKSPACE_OPERATION_RESULT,
                args=[inp, result],
                task_queue=queue,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_interval=timedelta(minutes=1)),
            )
        return result


__all__ = ["WorkspaceOperationWorkflow", "operation_workflow_id"]
