"""S-03 验收：取消、重复提交、只读 Task 并行与写 Task 串行，Continue-As-New 不丢 Task（计划 S-03 验收）。

端到端环境里只读 Task 并行上限为 2，每接受 3 个 Task 续接一次（e2e_support/stack.py）。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from temporalio.client import Client, WorkflowExecutionStatus

from agentruntime.workflow.v1 import session_pb2

from e2e_support.api import TaskApi, finished_payload, stream_until_finished
from e2e_support.stack import CONTINUE_AS_NEW_TASKS, READ_ONLY_LIMIT, Stack

pytestmark = pytest.mark.e2e


def finished(api: TaskApi, sub: dict, timeout: float = 90) -> dict:
    return finished_payload(stream_until_finished(sub["eventsUrl"], api.token or "", timeout=timeout))


def first_ts(stack: Stack, task_id: str, activity: str, event: str) -> float:
    return min(c["ts"] for c in stack.calls(task_id) if c["activity"] == activity and c["event"] == event)


async def session_state(stack: Stack, session_id: str) -> session_pb2.SessionState:
    client = await Client.connect(stack.temporal_address)
    return await client.get_workflow_handle(session_id).query("session_state", result_type=session_pb2.SessionState)


async def session_runs(stack: Stack, session_id: str) -> list[WorkflowExecutionStatus | None]:
    client = await Client.connect(stack.temporal_address)
    return [w.status async for w in client.list_workflows(f'WorkflowId = "{session_id}"')]


def test_write_tasks_run_one_at_a_time(stack: Stack, api: TaskApi) -> None:
    first = api.submit(stub_plan="slow_answer")
    second = api.submit(session_id=first["sessionId"])
    stack.wait_call(first["taskId"], "execute_model_node", "start")
    time.sleep(2)
    assert api.query(second["taskId"])["status"] == "TASK_STATUS_QUEUED"
    assert stack.count(second["taskId"], "plan_round", "start") == 0, "前一个写 Task 没结束，后一个不能开始"

    stack.open_gate(first["taskId"])
    assert finished(api, first)["status"] == "TASK_STATUS_DONE"
    assert finished(api, second)["status"] == "TASK_STATUS_DONE"
    assert first_ts(stack, second["taskId"], "plan_round", "start") >= first_ts(stack, first["taskId"], "finalize_task", "end")


def test_read_only_tasks_run_in_parallel_up_to_the_limit(stack: Stack, api: TaskApi) -> None:
    writer = api.submit(stub_plan="slow_answer")
    sid = writer["sessionId"]
    readers = [api.submit(session_id=sid, stub_plan="slow_answer", read_only=True) for _ in range(READ_ONLY_LIMIT + 1)]
    for r in readers[:READ_ONLY_LIMIT]:
        stack.wait_call(r["taskId"], "execute_model_node", "start")
    time.sleep(2)
    assert api.query(writer["taskId"])["status"] == "TASK_STATUS_RUNNING", "只读 Task 不等写 Task，写 Task 也照常运行"
    assert api.query(readers[-1]["taskId"])["status"] == "TASK_STATUS_QUEUED", "超过并行上限的只读 Task 排队"

    stack.open_gate(readers[0]["taskId"])
    assert finished(api, readers[0])["status"] == "TASK_STATUS_DONE"
    stack.wait_call(readers[-1]["taskId"], "execute_model_node", "start")
    for sub in [writer, *readers[1:]]:
        stack.open_gate(sub["taskId"])
    for sub in [writer, *readers[1:]]:
        assert finished(api, sub)["status"] == "TASK_STATUS_DONE"


def test_duplicate_submit_returns_the_same_task(stack: Stack, api: TaskApi) -> None:
    first = api.submit(client_request_id="dup-1")
    again = api.submit(client_request_id="dup-1")
    assert again["taskId"] == first["taskId"] and again["sessionId"] == first["sessionId"]
    assert again.get("deduplicated") is True
    in_session = api.submit(session_id=first["sessionId"], client_request_id="dup-2")
    in_session_again = api.submit(session_id=first["sessionId"], client_request_id="dup-2")
    assert in_session_again["taskId"] == in_session["taskId"] and in_session_again.get("deduplicated") is True
    for sub in (first, in_session):
        assert finished(api, sub)["status"] == "TASK_STATUS_DONE"
        assert stack.count(sub["taskId"], "plan_round", "start") == 1, "重复提交只跑一次"


def test_cancel_running_task(stack: Stack, api: TaskApi) -> None:
    sub = api.submit(stub_plan="slow_answer")
    stack.wait_call(sub["taskId"], "execute_model_node", "start")
    api.cancel(sub["taskId"], "用户取消")
    assert finished(api, sub)["status"] == "TASK_STATUS_CANCELLED"
    assert stack.count(sub["taskId"], "execute_model_node", "cancelled") == 1, "取消要传到正在运行的 activity"
    assert stack.count(sub["taskId"], "finalize_task", "end") == 1
    assert api.query(sub["taskId"])["status"] == "TASK_STATUS_CANCELLED"


def test_cancel_queued_task(stack: Stack, api: TaskApi) -> None:
    running = api.submit(stub_plan="slow_answer")
    queued = api.submit(session_id=running["sessionId"])
    stack.wait_call(running["taskId"], "execute_model_node", "start")
    api.cancel(queued["taskId"])
    assert finished(api, queued, timeout=30)["status"] == "TASK_STATUS_CANCELLED", "排队中的 Task 直接移出队列"
    assert stack.calls(queued["taskId"]) == [], "被取消的排队 Task 从未启动"
    assert api.query(queued["taskId"])["status"] == "TASK_STATUS_CANCELLED"
    stack.open_gate(running["taskId"])
    assert finished(api, running)["status"] == "TASK_STATUS_DONE"


def test_continue_as_new_keeps_every_task(stack: Stack, api: TaskApi) -> None:
    n = 8
    first = api.submit(client_request_id="can-0")
    sid = first["sessionId"]
    subs = [first] + [api.submit(session_id=sid, client_request_id=f"can-{i}") for i in range(1, n)]
    for sub in subs:
        assert finished(api, sub)["status"] == "TASK_STATUS_DONE"

    state = asyncio.run(session_state(stack, sid))
    assert state.accepted_tasks == n
    assert list(state.queued_task_ids) == [] and state.running_write_task_id == "" and list(state.running_read_only_task_ids) == []
    assert state.run_generation >= (n - 1) // CONTINUE_AS_NEW_TASKS
    runs = asyncio.run(session_runs(stack, sid))
    assert runs.count(WorkflowExecutionStatus.CONTINUED_AS_NEW) >= 2, f"Session 工作流应续接过，实际各次运行的状态 {runs}"
