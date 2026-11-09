"""Bounded file processing on the existing workspace queue; no auxiliary worker system."""
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
with workflow.unsafe.imports_passed_through():
    from google.protobuf.struct_pb2 import Struct
    from agentruntime_worker.workflows.patches import FILE_OPERATION_RESULT_PATCH

@workflow.defn(name="FileOperationWorkflow")
class FileOperationWorkflow:
    @workflow.run
    async def run(self, request: Struct) -> Struct:
        persist = workflow.patched(FILE_OPERATION_RESULT_PATCH)
        queue = workflow.info().task_queue if persist else "workspace"
        failed = False
        try:
            result = await workflow.execute_activity("file_operation", request, result_type=Struct,
                task_queue=queue, start_to_close_timeout=timedelta(minutes=7 if persist else 15),
                schedule_to_close_timeout=timedelta(minutes=16) if persist else None,
                heartbeat_timeout=timedelta(seconds=45),
                retry_policy=RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=3)))
        except ActivityError:
            failed = True
            result=Struct()
            result.update({"status":"failed","error":{"code":"file_operation_failed","message":"文件处理失败或超时，可重新开始处理"}})
        pending = result.fields.get("completion_pending")
        if persist and (failed or pending and pending.bool_value):
            # A separate bounded activity retries only the durable completion,
            # never repeats document parsing after an activity timeout.
            result = await workflow.execute_activity("file_operation_complete", args=[request, result], result_type=Struct,
                task_queue=queue, start_to_close_timeout=timedelta(minutes=2),
                schedule_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=3)))
        return result
