"""Bounded file processing on the existing workspace queue; no auxiliary worker system."""
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
with workflow.unsafe.imports_passed_through():
    from google.protobuf.struct_pb2 import Struct

@workflow.defn(name="FileOperationWorkflow")
class FileOperationWorkflow:
    @workflow.run
    async def run(self, request: Struct) -> Struct:
        try:
            return await workflow.execute_activity("file_operation", request, result_type=Struct,
                task_queue="workspace", start_to_close_timeout=timedelta(minutes=15),
                heartbeat_timeout=timedelta(seconds=45),
                retry_policy=RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=3)))
        except ActivityError:
            result=Struct()
            result.update({"status":"failed","error":{"code":"file_operation_failed","message":"文件处理失败或超时，可重新开始处理"}})
            return result
