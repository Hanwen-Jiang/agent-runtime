"""S-07 场景 6：同一信号重复到达；同一 Task 重复提交。

- 外部信号：同一个人工回复并发送达两次、Task 结束后又来一次；同一个取消并发两次。
- 内部信号：Task 子工作流发给 Session 工作流的 task_finished 重复到达，不能把写 Task 的串行锁释放两次。
- 重复提交：同一 client_request_id 并发提交多次，只有一个 Task、只跑一次（设计书 3.1.1）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.client import Client

from agentruntime.common.v1 import enums_pb2
from agentruntime.workflow.v1 import session_pb2, task_pb2

from e2e_support import history as h
from e2e_support.api import ApiError, TaskApi, stream_until_finished
from e2e_support.scenarios import answer_node, fault_stage, human_node, outcome, wait_human_ask
from e2e_support.stack import Stack

pytestmark = [pytest.mark.e2e, pytest.mark.faults, pytest.mark.scenario(6)]


def concurrently(n: int, fn):  # type: ignore[no-untyped-def]
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(fn, i) for i in range(n)]
        return [f.result() for f in futures]


def event_names(api: TaskApi, sub: dict) -> list[str]:
    return [e.event for e in stream_until_finished(sub["eventsUrl"], api.token or "")]


def test_duplicate_human_replies_are_applied_once(fstack: Stack, api: TaskApi) -> None:
    sub = api.submit(inputs=fault_stage(human_node("confirm"), answer_node(after=("confirm",))))
    task_id = sub["taskId"]
    wait_human_ask(fstack, task_id, "confirm")

    concurrently(2, lambda i: api.reply(task_id, "confirm", "approve", f"第 {i} 份"))
    assert outcome(api, sub)["status"] == "TASK_STATUS_DONE"
    with pytest.raises(ApiError) as late:
        api.reply(task_id, "confirm", "approve", "Task 结束后才到")
    assert late.value.code == "failed_precondition"

    assert fstack.count(task_id, "execute_model_node", "end") == 1
    assert event_names(api, sub).count("task_finished") == 1
    task = h.fetch_one(fstack.temporal_address, task_id)
    report = h.analyze(task)
    h.assert_clean(report)
    assert report.signals.count("human_reply") == 2, "两份回复都记入历史，只有一份生效"
    h.assert_replays_python([task])


async def _signal_task_finished(address: str, session_id: str, result: task_pb2.TaskWorkflowResult, times: int) -> None:
    client = await Client.connect(address)
    handle = client.get_workflow_handle(session_id)
    for _ in range(times):
        await handle.signal("task_finished", result)


async def _session_state(address: str, session_id: str) -> session_pb2.SessionState:
    client = await Client.connect(address)
    return await client.get_workflow_handle(session_id).query("session_state", result_type=session_pb2.SessionState)


def test_duplicate_task_finished_does_not_release_the_write_lock_twice(fstack: Stack, api: TaskApi) -> None:
    first = api.submit()
    sid = first["sessionId"]
    assert outcome(api, first)["status"] == "TASK_STATUS_DONE"
    first_last_event = api.query(first["taskId"])["lastEventId"]

    running = api.submit(session_id=sid, stub_plan="slow_answer")
    fstack.wait_call(running["taskId"], "execute_model_node", "start")
    queued = api.submit(session_id=sid)

    # 已结束的写 Task 的 task_finished 又到了两次（例如子工作流重试发送）。
    done = task_pb2.TaskWorkflowResult(task_id=first["taskId"], status=enums_pb2.TASK_STATUS_DONE)
    asyncio.run(_signal_task_finished(fstack.temporal_address, sid, done, times=2))
    time.sleep(2)

    state = asyncio.run(_session_state(fstack.temporal_address, sid))
    assert state.running_write_task_id == running["taskId"], "重复的 task_finished 不影响正在运行的写 Task"
    assert list(state.queued_task_ids) == [queued["taskId"]], "后面的写 Task 仍在排队"
    assert fstack.count(queued["taskId"], "plan_round", "start") == 0
    assert api.query(first["taskId"])["lastEventId"] == first_last_event, "已结束的 Task 不再推送结束事件"

    fstack.open_gate(running["taskId"])
    assert outcome(api, running)["status"] == "TASK_STATUS_DONE"
    assert outcome(api, queued)["status"] == "TASK_STATUS_DONE"
    started = min(c["ts"] for c in fstack.calls(queued["taskId"]) if c["activity"] == "plan_round" and c["event"] == "start")
    released = max(c["ts"] for c in fstack.calls(running["taskId"]) if c["activity"] == "finalize_task" and c["event"] == "end")
    assert started >= released, "写 Task 依然串行"

    runs = h.fetch(fstack.temporal_address, sid)
    h.assert_session_clean(runs)
    assert sum(r.signals.count("task_finished") for r in map(h.analyze, runs)) >= 2
    h.assert_replays_go(fstack.task_service_bin, runs)


def test_concurrent_duplicate_submits_create_one_task(fstack: Stack, api: TaskApi) -> None:
    crid = f"dup-{uuid.uuid4().hex}"
    subs = concurrently(8, lambda i: api.submit(client_request_id=crid))
    task_ids = {s["taskId"] for s in subs}
    session_ids = {s["sessionId"] for s in subs}
    assert len(task_ids) == 1 and len(session_ids) == 1, f"同一 client_request_id 只对应一个 Task：{task_ids} {session_ids}"
    assert sum(1 for s in subs if not s.get("deduplicated")) == 1, "只有一次提交真正创建了 Task"

    sub = subs[0]
    assert outcome(api, sub)["status"] == "TASK_STATUS_DONE"
    again = api.submit(client_request_id=crid)
    assert again["taskId"] == sub["taskId"] and again.get("deduplicated") is True, "结束之后重复提交也返回同一个 Task"

    task_id = sub["taskId"]
    assert fstack.count(task_id, "plan_round", "start") == 1
    names = event_names(api, sub)
    assert names.count("task_accepted") == 1 and names.count("task_finished") == 1
    h.assert_clean(h.analyze(h.fetch_one(fstack.temporal_address, task_id)))


def test_duplicate_cancels_are_idempotent(fstack: Stack, api: TaskApi) -> None:
    sub = api.submit(stub_plan="slow_answer")
    task_id = sub["taskId"]
    fstack.wait_call(task_id, "execute_model_node", "start")
    concurrently(2, lambda i: api.cancel(task_id, f"第 {i} 次取消"))

    assert outcome(api, sub)["status"] == "TASK_STATUS_CANCELLED"
    assert fstack.count(task_id, "execute_model_node", "cancelled") == 1
    assert fstack.count(task_id, "finalize_task", "end") == 1
    assert event_names(api, sub).count("task_finished") == 1
    task = h.fetch_one(fstack.temporal_address, task_id)
    h.assert_clean(h.analyze(task), close="canceled")
    h.assert_replays_python([task])
