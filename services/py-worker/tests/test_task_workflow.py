"""S-03 验收：Task 工作流（设计书 3.2.1、附录 K.1；计划 S-03 交付“Task 工作流”与难点“取消”）。

在 Temporal 本地开发服务上跑真实的 Task 工作流与桩 activity。Go 的 Session 工作流用 Python 的
StandInSession 代替：它把 Task 作为子工作流启动，并接收 task_finished 信号。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from google.protobuf import json_format
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.exceptions import CancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentruntime.common.v1 import enums_pb2
from agentruntime.workflow.v1 import task_pb2

from agentruntime_worker import names
from agentruntime_worker.activities.calllog import read_calls
from agentruntime_worker.activities.stubs import StubActivities
from agentruntime_worker.activities.types import PlanRoundInput, ReviewRoundInput, StageResult
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.worker import QUEUE_COMPONENTS, build_workers

from .support import assert_valid, task_input

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

SESSION_QUEUE = "session"


@workflow.defn(name=names.SESSION_WORKFLOW, sandboxed=False)
class StandInSession:
    """Go 的 Session 工作流的替身：启动 Task 子工作流，等它结束，返回收到的第一个 task_finished。"""

    def __init__(self) -> None:
        self._finished: list[task_pb2.TaskWorkflowResult] = []

    @workflow.run
    async def run(self, task: task_pb2.TaskWorkflowInput) -> task_pb2.TaskWorkflowResult:
        child = await workflow.start_child_workflow(
            names.TASK_WORKFLOW,
            task,
            id=task.task_id,
            task_queue=names.TASK_QUEUE,
            parent_close_policy=workflow.ParentClosePolicy.ABANDON,
        )
        with contextlib.suppress(Exception):
            await child
        await workflow.wait_condition(lambda: bool(self._finished), timeout=timedelta(seconds=30))
        return self._finished[0]

    @workflow.signal(name=names.TASK_FINISHED_SIGNAL)
    def task_finished(self, result: task_pb2.TaskWorkflowResult) -> None:
        self._finished.append(result)

    @workflow.query(name="finished_count")
    def finished_count(self) -> int:
        return len(self._finished)


class RecordingStubs(StubActivities):
    """记下 Planner 与 Reviewer 收到的输入，其余照桩执行。"""

    def __init__(self, config: WorkerConfig) -> None:
        super().__init__(config)
        self.plan_inputs: list[PlanRoundInput] = []
        self.review_inputs: list[ReviewRoundInput] = []

    @activity.defn(name=names.PLAN_ROUND)
    async def plan_round(self, inp: PlanRoundInput) -> Any:
        self.plan_inputs.append(inp)
        return await super().plan_round(inp)

    @activity.defn(name=names.REVIEW_ROUND)
    async def review_round(self, inp: ReviewRoundInput) -> Any:
        self.review_inputs.append(inp)
        return await super().review_round(inp)


class Harness:
    def __init__(self, client: Client, stubs: RecordingStubs, config: WorkerConfig) -> None:
        self.client = client
        self.stubs = stubs
        self.config = config
        self.handles: list[WorkflowHandle[Any, Any]] = []

    async def start_via_session(self, inp: task_pb2.TaskWorkflowInput) -> tuple[WorkflowHandle[Any, Any], WorkflowHandle[Any, Any]]:
        session = await self.client.start_workflow(
            names.SESSION_WORKFLOW,
            inp,
            id=f"session-for-{inp.task_id}",
            task_queue=SESSION_QUEUE,
            result_type=task_pb2.TaskWorkflowResult,
        )
        self.handles.append(session)
        task = self.client.get_workflow_handle(inp.task_id, result_type=task_pb2.TaskWorkflowResult)
        self.handles.append(task)
        await self.wait_started(task)
        return session, task

    async def start_direct(self, inp: task_pb2.TaskWorkflowInput) -> WorkflowHandle[Any, Any]:
        task = await self.client.start_workflow(
            names.TASK_WORKFLOW, inp, id=inp.task_id, task_queue=names.TASK_QUEUE, result_type=task_pb2.TaskWorkflowResult
        )
        self.handles.append(task)
        return task

    async def wait_started(self, handle: WorkflowHandle[Any, Any], timeout: float = 15) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                await handle.describe()
                return
            except Exception:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.1)

    def calls(self, task_id: str) -> list[dict[str, Any]]:
        assert self.config.call_log is not None
        return [c for c in read_calls(self.config.call_log) if c["task_id"] == task_id]

    def events(self, task_id: str, activity_name: str, event: str) -> list[dict[str, Any]]:
        return [c for c in self.calls(task_id) if c["activity"] == activity_name and c["event"] == event]

    async def wait_call(self, task_id: str, activity_name: str, event: str, timeout: float = 15) -> None:
        deadline = time.monotonic() + timeout
        while not self.events(task_id, activity_name, event):
            if time.monotonic() > deadline:
                pytest.fail(f"{timeout} 秒内没有看到 {activity_name} 的 {event}：{self.calls(task_id)}")
            await asyncio.sleep(0.1)

    def open_gate(self, task_id: str) -> None:
        assert self.config.gate_dir is not None
        (self.config.gate_dir / task_id).touch()

    async def task_state(self, handle: WorkflowHandle[Any, Any]) -> tuple[task_pb2.TaskStateQueryResult, dict[str, Any]]:
        result = await handle.query(names.TASK_STATE_QUERY, result_type=task_pb2.TaskStateQueryResult)
        return result, json_format.MessageToDict(result.task_state.value)

    async def terminate_all(self) -> None:
        for handle in self.handles:
            with contextlib.suppress(Exception):
                await handle.terminate("测试结束")


@pytest_asyncio.fixture(loop_scope="session")
async def harness(temporal_env: WorkflowEnvironment, tmp_path: Path) -> AsyncIterator[Harness]:
    (tmp_path / "gates").mkdir()
    config = WorkerConfig(call_log=tmp_path / "calls.jsonl", gate_dir=tmp_path / "gates", stub_node_timeout_s=20)
    stubs = RecordingStubs(config)
    client = temporal_env.client
    # 按 Worker 启动入口的登记方式：每个队列登记 QUEUE_COMPONENTS 列出的工作流与 activity（含 local activity）。
    workers = [
        Worker(client, task_queue=SESSION_QUEUE, workflows=[StandInSession]),
        *build_workers(client, config, list(QUEUE_COMPONENTS), stubs=stubs),
    ]
    h = Harness(client, stubs, config)
    async with contextlib.AsyncExitStack() as stack:
        for w in workers:
            await stack.enter_async_context(w)
        try:
            yield h
        finally:
            await h.terminate_all()


def started(h: Harness, task_id: str) -> list[str]:
    return [c["activity"] for c in h.calls(task_id) if c["event"] == "start"]


async def expect_cancelled(handle: WorkflowHandle[Any, Any]) -> None:
    with pytest.raises(WorkflowFailureError) as err:
        await asyncio.wait_for(handle.result(), timeout=30)
    assert isinstance(err.value.cause, CancelledError), f"Task 工作流应以取消结束，实际 {err.value.cause!r}"


# --- Round 循环 ---


async def test_round_runs_planner_stage_reviewer_then_finalizes(harness: Harness) -> None:
    inp = task_input(stub_plan="answer")
    _, task = await harness.start_via_session(inp)
    result = await asyncio.wait_for(task.result(), timeout=30)

    assert result.task_id == inp.task_id
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert result.final_verdict == enums_pb2.VERDICT_DONE
    assert result.deliver_node_id == "answer"
    assert result.rounds == 1
    assert json_format.MessageToDict(result.deliverable.value)["text"]
    assert started(harness, inp.task_id) == [names.PLAN_ROUND, names.EXECUTE_MODEL_NODE, names.REVIEW_ROUND, names.FINALIZE_TASK]


async def test_activities_run_on_their_resource_queues(harness: Harness) -> None:
    inp = task_input(stub_plan="answer")
    _, task = await harness.start_via_session(inp)
    await asyncio.wait_for(task.result(), timeout=30)
    queues = {c["activity"]: c["task_queue"] for c in harness.calls(inp.task_id)}
    assert queues == {
        names.PLAN_ROUND: names.DECISION_QUEUE,
        names.REVIEW_ROUND: names.DECISION_QUEUE,
        names.FINALIZE_TASK: names.DECISION_QUEUE,
        names.EXECUTE_MODEL_NODE: names.MODEL_QUEUE,
    }


async def test_continue_verdict_starts_another_round(harness: Harness) -> None:
    inp = task_input(stub_plan="answer", stub_rounds=2)
    _, task = await harness.start_via_session(inp)
    result = await asyncio.wait_for(task.result(), timeout=30)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert result.rounds == 2
    assert [p.round_seq for p in harness.stubs.plan_inputs if p.task_id == inp.task_id] == [1, 2]
    _, state = await harness.task_state(task)
    assert [(r["seq"], r["status"], r["verdict"]) for r in state["rounds"]] == [(1, "completed", "continue"), (2, "completed", "done")]


# --- 通知 Session 工作流 ---


async def test_session_is_notified_once_with_the_result(harness: Harness) -> None:
    inp = task_input(stub_plan="answer")
    session, task = await harness.start_via_session(inp)
    result = await asyncio.wait_for(task.result(), timeout=30)
    notified = await asyncio.wait_for(session.result(), timeout=30)
    assert notified == result, "task_finished 的载荷应就是 Task 工作流的结果"
    assert await session.query("finished_count", result_type=int) == 1


async def test_task_without_a_parent_session_still_completes(harness: Harness) -> None:
    inp = task_input(stub_plan="answer")
    task = await harness.start_direct(inp)
    result = await asyncio.wait_for(task.result(), timeout=30)
    assert result.status == enums_pb2.TASK_STATUS_DONE


# --- Task State 与版本固定 ---


async def test_task_state_is_queryable_and_matches_the_schema(harness: Harness) -> None:
    inp = task_input(stub_plan="slow_answer", done_when="给出可执行的建议")
    _, task = await harness.start_via_session(inp)
    await harness.wait_call(inp.task_id, names.EXECUTE_MODEL_NODE, "start")

    running, state = await harness.task_state(task)
    assert running.task_id == inp.task_id
    assert running.status == enums_pb2.TASK_STATUS_RUNNING
    assert running.round_seq == 1
    assert running.task_state.schema_id == names.TASK_STATE_SCHEMA_ID
    assert_valid("task_state", state)
    assert state["task_id"] == inp.task_id and state["session_id"] == inp.tenant.session_id
    assert state["goal"] == inp.message
    assert state["done_when"] == "给出可执行的建议"
    assert state["status"] == "running"
    assert state["constraints"] == {"effect_ceiling": "read", "budget_usd": 2.0}
    assert state["rounds"][-1]["seq"] == 1 and state["rounds"][-1]["status"] == "running"
    assert "verdict" not in state["rounds"][-1]
    assert state["pinned"] == {
        "recipe": "stub@v1",
        "node_templates": {"stub_answer": "v2"},
        "reasoning_templates": {"direct": "v1"},
        "model_profiles": {"mock/model": "v3"},
    }

    harness.open_gate(inp.task_id)
    await asyncio.wait_for(task.result(), timeout=30)
    done, state = await harness.task_state(task)
    assert done.status == enums_pb2.TASK_STATUS_DONE
    assert_valid("task_state", state)
    assert state["status"] == "done"
    assert state["rounds"][-1] == {**state["rounds"][-1], "status": "completed", "verdict": "done"}


async def test_pinned_versions_reach_the_planner(harness: Harness) -> None:
    inp = task_input(stub_plan="answer")
    _, task = await harness.start_via_session(inp)
    await asyncio.wait_for(task.result(), timeout=30)
    plans = [p for p in harness.stubs.plan_inputs if p.task_id == inp.task_id]
    assert plans and plans[0].pinned == {
        "recipe": "stub@v1",
        "node_templates": {"stub_answer": "v2"},
        "reasoning_templates": {"direct": "v1"},
        "model_profiles": {"mock/model": "v3"},
        "tenant_policy": "v5",
    }
    assert plans[0].tenant.tenant_id == "t1" and plans[0].tenant.session_id == inp.tenant.session_id


# --- Reviewer 的输入 ---


async def test_reviewer_sees_only_results_state_and_criteria(harness: Harness) -> None:
    inp = task_input(stub_plan="answer", done_when="每个结论有数据支撑")
    _, task = await harness.start_via_session(inp)
    await asyncio.wait_for(task.result(), timeout=30)
    reviews = [r for r in harness.stubs.review_inputs if r.task_id == inp.task_id]
    assert len(reviews) == 1
    review = reviews[0]
    # S-10：另带本轮执行的 Stage（判 continue 要看它的 exit_when，诊断形状要看 Node 结构）与之前各 Round 的摘要；
    # 仍然没有 Task Plan、Triage 的理由与任何 rationale。
    assert {f.name for f in dataclasses.fields(review)} == {"tenant", "task_id", "round_seq", "task_state", "stage_results", "done_when", "stage", "history"}
    assert review.done_when == "每个结论有数据支撑" == review.task_state["done_when"]
    assert set(review.stage_results) == {"answer"}
    answer = review.stage_results["answer"]
    assert isinstance(answer, StageResult)
    assert answer.role == "deliver" and answer.status == "succeeded" and answer.result["text"]
    assert review.stage is not None and review.stage["exit_when"] and review.history == []
    dumped = json.dumps(dataclasses.asdict(review), ensure_ascii=False)
    assert "rationale" not in dumped, "Node 的 rationale 只写入 trace，不传给下游"
    assert "task_plan" not in dumped, "Reviewer 不看 Planner 的规划过程"
    results_dump = json.dumps({k: dataclasses.asdict(v) for k, v in review.stage_results.items()}, ensure_ascii=False)
    assert "triage" not in results_dump and "template" not in results_dump


# --- 取消 ---


async def test_cancel_reaches_the_running_activity_and_finalizes_once(harness: Harness) -> None:
    inp = task_input(stub_plan="slow_answer")
    session, task = await harness.start_via_session(inp)
    await harness.wait_call(inp.task_id, names.EXECUTE_MODEL_NODE, "start")
    await task.cancel()
    await expect_cancelled(task)

    assert harness.events(inp.task_id, names.EXECUTE_MODEL_NODE, "cancelled"), "取消要传到正在运行的 activity"
    assert not harness.events(inp.task_id, names.EXECUTE_MODEL_NODE, "end")
    assert not harness.events(inp.task_id, names.REVIEW_ROUND, "start")
    assert len(harness.events(inp.task_id, names.FINALIZE_TASK, "start")) == 1
    assert len(harness.events(inp.task_id, names.FINALIZE_TASK, "end")) == 1, "收尾写回不可取消，且只做一次"
    calls = harness.calls(inp.task_id)
    cancelled_at = next(i for i, c in enumerate(calls) if c["event"] == "cancelled")
    finalize_at = next(i for i, c in enumerate(calls) if c["activity"] == names.FINALIZE_TASK)
    assert cancelled_at < finalize_at, "等 activity 确认取消之后再收尾"

    notified = await asyncio.wait_for(session.result(), timeout=30)
    assert notified.status == enums_pb2.TASK_STATUS_CANCELLED
    _, state = await harness.task_state(task)
    assert state["status"] == "cancelled"


async def test_cancel_ends_the_human_wait(harness: Harness) -> None:
    inp = task_input(stub_plan="human_gate")
    session, task = await harness.start_via_session(inp)
    await harness.wait_call(inp.task_id, names.PLAN_ROUND, "end")
    await asyncio.sleep(0.5)
    await task.cancel()
    await expect_cancelled(task)
    assert not harness.events(inp.task_id, names.EXECUTE_MODEL_NODE, "start"), "gate 之后的节点不应执行"
    assert len(harness.events(inp.task_id, names.FINALIZE_TASK, "end")) == 1
    assert (await asyncio.wait_for(session.result(), timeout=30)).status == enums_pb2.TASK_STATUS_CANCELLED


# --- 人工回复 ---


async def test_human_reply_resumes_the_stage(harness: Harness) -> None:
    inp = task_input(stub_plan="human_gate")
    _, task = await harness.start_via_session(inp)
    await harness.wait_call(inp.task_id, names.PLAN_ROUND, "end")

    # 不是问题给出的选项、或问的不是这个节点：忽略，继续等。
    await task.signal(names.HUMAN_REPLY_SIGNAL, task_pb2.HumanReplySignal(question_id="confirm", option="maybe"))
    await task.signal(names.HUMAN_REPLY_SIGNAL, task_pb2.HumanReplySignal(question_id="other", option="approve"))
    await asyncio.sleep(1)
    running, _ = await harness.task_state(task)
    assert running.status == enums_pb2.TASK_STATUS_RUNNING
    assert not harness.events(inp.task_id, names.EXECUTE_MODEL_NODE, "start")

    await task.signal(
        names.HUMAN_REPLY_SIGNAL,
        task_pb2.HumanReplySignal(question_id="confirm", option="approve", guidance="只看华东", replied_by_user_id="u7"),
    )
    result = await asyncio.wait_for(task.result(), timeout=30)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    review = next(r for r in harness.stubs.review_inputs if r.task_id == inp.task_id)
    assert review.stage_results["confirm"].result == {"option": "approve", "guidance": "只看华东", "replied_by": "u7"}


# --- 失败 ---


async def test_failed_node_goes_to_the_reviewer_and_still_wraps_up(harness: Harness) -> None:
    """S-04 起 Node 失败交给 Reviewer（附录 D.6）；桩 Reviewer 看到交付节点失败判 abort。"""
    inp = task_input(stub_plan="fail_model")
    session, task = await harness.start_via_session(inp)
    result = await asyncio.wait_for(task.result(), timeout=30)
    assert result.status == enums_pb2.TASK_STATUS_ABORTED
    assert result.final_verdict == enums_pb2.VERDICT_ABORT
    review = next(r for r in harness.stubs.review_inputs if r.task_id == inp.task_id)
    assert review.stage_results["answer"].status == "failed" and review.stage_results["answer"].error
    assert len(harness.events(inp.task_id, names.FINALIZE_TASK, "end")) == 1
    assert (await asyncio.wait_for(session.result(), timeout=30)).status == enums_pb2.TASK_STATUS_ABORTED
