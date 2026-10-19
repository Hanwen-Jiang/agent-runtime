"""DurableEngine 一致性测试用的 Temporal 工作流，语义见 agentruntime_core.conformance.durable_engine。

单独成模块，只依赖 temporalio 与 protobuf，方便 Temporal 的工作流沙箱加载。
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from google.protobuf.wrappers_pb2 import StringValue


@workflow.defn(name="conformance.Echo")
class EchoWorkflow:
    @workflow.init
    def __init__(self, start: StringValue) -> None:
        self._values = [start.value]
        self._finished = False

    @workflow.run
    async def run(self, start: StringValue) -> StringValue:
        await workflow.wait_condition(lambda: self._finished)
        return StringValue(value=",".join(self._values))

    @workflow.signal(name="append")
    def append(self, value: StringValue) -> None:
        self._values.append(value.value)

    @workflow.signal(name="finish")
    def finish(self) -> None:
        self._finished = True

    @workflow.query(name="state")
    def state(self) -> StringValue:
        return StringValue(value=",".join(self._values))


@workflow.defn(name="conformance.Fail")
class FailWorkflow:
    @workflow.run
    async def run(self) -> StringValue:
        raise ApplicationError("boom", non_retryable=True)
