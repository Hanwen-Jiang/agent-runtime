"""DurableEngine 的一致性测试。语义见 agentruntime_core.providers.durable_engine。

harness 必须在 task_queue 上注册两个测试工作流（工作流代码由 harness 用所在语言的 Temporal SDK 实现）：

- conformance.Echo：输入 StringValue。信号 append（StringValue）把值追加到列表；查询 state 返回 StringValue，
  内容是输入与已追加的值用逗号连接；信号 finish 让工作流结束，结果为同样的 StringValue。被取消时以取消结束。
- conformance.Fail：启动后立即失败，失败信息为 boom。
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Protocol

import pytest
from google.protobuf.wrappers_pb2 import StringValue

from agentruntime_core.conformance.common import Harness, StandardRules, expect_error, tenant_a, tenant_b
from agentruntime_core.errors import ErrorCategory, NotFoundError
from agentruntime_core.providers.durable_engine import DurableEngine, DurableEngineProvider, WorkflowStatus

ECHO = "conformance.Echo"
FAIL = "conformance.Fail"


class DurableEngineHarness(Harness[DurableEngineProvider], Protocol):
    @property
    def task_queue(self) -> str:
        """测试工作流所在的任务队列。"""
        ...


def _id(label: str) -> str:
    # 开发服务在测试之间保留历史，工作流 ID 每次都要新的。
    return f"conformance-{label}-{uuid.uuid4().hex[:12]}"


class DurableEngineConformance(StandardRules):
    probe_operation = "start_workflow"

    def _probe_ids(self) -> dict[str, str]:
        return self.__dict__.setdefault("_ids", {})

    async def write_probe(self, client: DurableEngine, name: str) -> None:
        workflow_id = self._probe_ids().setdefault(name, _id(name))
        harness_queue = self.__dict__["_queue"]
        await client.start_workflow(ECHO, workflow_id=workflow_id, task_queue=harness_queue, input=StringValue(value=name))

    async def probe_visible(self, client: DurableEngine, name: str) -> bool:
        try:
            await client.describe(self._probe_ids()[name])
        except NotFoundError:
            return False
        return True

    @pytest.fixture(autouse=True)
    def _remember_queue(self, harness: DurableEngineHarness) -> None:
        self.__dict__["_queue"] = harness.task_queue

    @pytest.fixture
    def engine(self, harness: DurableEngineHarness) -> DurableEngine:
        return harness.provider.bind(tenant_a())

    async def _start_echo(self, engine: DurableEngine, harness: DurableEngineHarness, label: str) -> str:
        workflow_id = _id(label)
        await engine.start_workflow(ECHO, workflow_id=workflow_id, task_queue=harness.task_queue, input=StringValue(value="start"))
        return workflow_id

    async def test_start_and_describe(self, harness: DurableEngineHarness, engine: DurableEngine) -> None:
        workflow_id = _id("describe")
        run = await engine.start_workflow(ECHO, workflow_id=workflow_id, task_queue=harness.task_queue, input=StringValue(value="start"))
        assert run.workflow_id == workflow_id and run.run_id and run.already_started is False
        description = await engine.describe(workflow_id)
        assert description.status is WorkflowStatus.RUNNING
        assert description.workflow_type == ECHO
        assert description.task_queue == harness.task_queue
        assert description.run_id == run.run_id

    async def test_start_is_idempotent_by_workflow_id(self, harness: DurableEngineHarness, engine: DurableEngine) -> None:
        workflow_id = _id("idempotent")
        first = await engine.start_workflow(ECHO, workflow_id=workflow_id, task_queue=harness.task_queue, input=StringValue(value="a"))
        second = await engine.start_workflow(ECHO, workflow_id=workflow_id, task_queue=harness.task_queue, input=StringValue(value="b"))
        assert second.already_started is True
        assert second.run_id == first.run_id
        state = await engine.query(workflow_id, "state", StringValue)
        assert state.value == "a", "重复启动不应启动新运行，也不应改变输入"

    async def test_start_same_id_with_other_type_conflicts(self, harness: DurableEngineHarness, engine: DurableEngine) -> None:
        workflow_id = await self._start_echo(engine, harness, "type-conflict")
        with expect_error(ErrorCategory.CONFLICT):
            await engine.start_workflow(FAIL, workflow_id=workflow_id, task_queue=harness.task_queue)

    async def test_signal_then_query(self, harness: DurableEngineHarness, engine: DurableEngine) -> None:
        workflow_id = await self._start_echo(engine, harness, "signal")
        await engine.signal(workflow_id, "append", StringValue(value="x"))
        await engine.signal(workflow_id, "append", StringValue(value="y"))
        assert (await engine.query(workflow_id, "state", StringValue)).value == "start,x,y"

    async def test_signal_with_start_creates_then_reuses(self, harness: DurableEngineHarness, engine: DurableEngine) -> None:
        workflow_id = _id("signal-with-start")
        first = await engine.signal_with_start(
            ECHO, workflow_id=workflow_id, task_queue=harness.task_queue, input=StringValue(value="s"),
            signal="append", payload=StringValue(value="1"),
        )
        second = await engine.signal_with_start(
            ECHO, workflow_id=workflow_id, task_queue=harness.task_queue, input=StringValue(value="ignored"),
            signal="append", payload=StringValue(value="2"),
        )
        assert first.already_started is False and second.already_started is True
        assert (await engine.query(workflow_id, "state", StringValue)).value == "s,1,2"

    async def test_result_of_completed_workflow(self, harness: DurableEngineHarness, engine: DurableEngine) -> None:
        workflow_id = await self._start_echo(engine, harness, "result")
        await engine.signal(workflow_id, "append", StringValue(value="done"))
        await engine.signal(workflow_id, "finish")
        outcome = await engine.result(workflow_id, StringValue, timeout=timedelta(seconds=30))
        assert outcome.status is WorkflowStatus.COMPLETED
        assert outcome.value is not None and outcome.value.value == "start,done"
        assert (await engine.describe(workflow_id)).status is WorkflowStatus.COMPLETED

    async def test_result_of_failed_workflow(self, harness: DurableEngineHarness, engine: DurableEngine) -> None:
        workflow_id = _id("fail")
        await engine.start_workflow(FAIL, workflow_id=workflow_id, task_queue=harness.task_queue)
        outcome = await engine.result(workflow_id, StringValue, timeout=timedelta(seconds=30))
        assert outcome.status is WorkflowStatus.FAILED
        assert outcome.value is None
        assert "boom" in outcome.failure

    async def test_result_wait_timeout_is_transient(self, harness: DurableEngineHarness, engine: DurableEngine) -> None:
        workflow_id = await self._start_echo(engine, harness, "wait")
        with expect_error(ErrorCategory.TRANSIENT):
            await engine.result(workflow_id, StringValue, timeout=timedelta(milliseconds=300))

    async def test_cancel_is_idempotent(self, harness: DurableEngineHarness, engine: DurableEngine) -> None:
        workflow_id = await self._start_echo(engine, harness, "cancel")
        await engine.cancel(workflow_id, reason="user stopped")
        outcome = await engine.result(workflow_id, StringValue, timeout=timedelta(seconds=30))
        assert outcome.status is WorkflowStatus.CANCELLED
        await engine.cancel(workflow_id, reason="again")

    async def test_unknown_workflow_is_not_found(self, engine: DurableEngine) -> None:
        missing = _id("missing")
        with expect_error(ErrorCategory.NOT_FOUND):
            await engine.signal(missing, "append", StringValue(value="x"))
        with expect_error(ErrorCategory.NOT_FOUND):
            await engine.query(missing, "state", StringValue)
        with expect_error(ErrorCategory.NOT_FOUND):
            await engine.describe(missing)
        with expect_error(ErrorCategory.NOT_FOUND):
            await engine.cancel(missing)

    async def test_other_tenant_cannot_touch_workflow(self, harness: DurableEngineHarness, engine: DurableEngine) -> None:
        workflow_id = await self._start_echo(engine, harness, "foreign")
        b = harness.provider.bind(tenant_b())
        with expect_error(ErrorCategory.NOT_FOUND):
            await b.signal(workflow_id, "append", StringValue(value="evil"))
        with expect_error(ErrorCategory.NOT_FOUND):
            await b.query(workflow_id, "state", StringValue)
        with expect_error(ErrorCategory.NOT_FOUND):
            await b.cancel(workflow_id)
        with expect_error(ErrorCategory.NOT_FOUND):
            await b.result(workflow_id, StringValue, timeout=timedelta(seconds=1))
        with expect_error(ErrorCategory.CONFLICT):
            await b.start_workflow(ECHO, workflow_id=workflow_id, task_queue=harness.task_queue, input=StringValue(value="x"))
        assert (await engine.query(workflow_id, "state", StringValue)).value == "start"
        assert (await engine.describe(workflow_id)).status is WorkflowStatus.RUNNING

    @pytest.mark.parametrize("workflow_id", ["", "x" * 256, "bad\nid"], ids=["empty", "too_long", "newline"])
    async def test_malformed_workflow_id_is_invalid(self, harness: DurableEngineHarness, engine: DurableEngine, workflow_id: str) -> None:
        with expect_error(ErrorCategory.INVALID):
            await engine.start_workflow(ECHO, workflow_id=workflow_id, task_queue=harness.task_queue)
