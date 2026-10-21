"""S-07 场景 8：Session 工作流 Continue-As-New 时有信号到达。

端到端环境里 Session 每接受 3 个 Task 续接一次（e2e_support/stack.py）。一个写 Task 运行期间，并发地提交一批
只读与写 Task、取消其中两个排队的写 Task；提交信号成批到达，跨过好几次续接。之后放行最早的写 Task，它的 task_finished
在续接之后才到（上一次运行启动的子工作流）。断言：
- 每个提交都被接受且只接受一次，每个 Task 只有一个结局；被取消的排队 Task 从未启动；
- 写 Task 依然串行，只读 Task 的并行数不超过上限；
- Session 的每一次运行都没有异常，用当前代码能回放。
"""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.client import Client

from agentruntime.workflow.v1 import session_pb2

from e2e_support import history as h
from e2e_support.api import TaskApi
from e2e_support.scenarios import outcome
from e2e_support.stack import CONTINUE_AS_NEW_TASKS, READ_ONLY_LIMIT, Stack

pytestmark = [pytest.mark.e2e, pytest.mark.faults, pytest.mark.scenario(8)]

READERS = 6
WRITERS = 4
CANCELLED = 2


async def _session_state(address: str, session_id: str) -> session_pb2.SessionState:
    client = await Client.connect(address)
    return await client.get_workflow_handle(session_id).query("session_state", result_type=session_pb2.SessionState)


def interval(stack: Stack, task_id: str) -> tuple[float, float]:
    """Task 占着 Session 的时段：从第一次规划到交付（S-10 起收尾写回在交付之后异步进行，不算在内）。"""
    start = min(c["ts"] for c in stack.calls(task_id) if c["activity"] == "plan_round" and c["event"] == "start")
    return start, stack.handed_off_at(task_id)


def max_overlap(intervals: list[tuple[float, float]]) -> int:
    points = sorted([(s, 1) for s, _ in intervals] + [(e, -1) for _, e in intervals], key=lambda p: (p[0], p[1]))
    best = current = 0
    for _, delta in points:
        current += delta
        best = max(best, current)
    return best


def test_signals_arriving_during_continue_as_new_are_not_lost(fstack: Stack, api: TaskApi) -> None:
    blocker = api.submit(stub_plan="slow_answer")
    sid = blocker["sessionId"]
    fstack.wait_call(blocker["taskId"], "execute_model_node", "start")

    kinds = ["read"] * READERS + ["write"] * WRITERS
    with ThreadPoolExecutor(max_workers=len(kinds)) as pool:
        subs = list(pool.map(lambda kind: (kind, api.submit(session_id=sid, read_only=kind == "read", client_request_id=uuid.uuid4().hex)), kinds))
    writers = [s for kind, s in subs if kind == "write"]
    readers = [s for kind, s in subs if kind == "read"]
    # 写 Task 都排在 blocker 后面，取消其中两个时它们一定还在排队。
    cancelled = writers[:CANCELLED]
    with ThreadPoolExecutor(max_workers=CANCELLED) as pool:
        list(pool.map(lambda s: api.cancel(s["taskId"], "排队中取消"), cancelled))

    fstack.open_gate(blocker["taskId"])
    everyone = [blocker] + [s for _, s in subs]
    assert len({s["taskId"] for s in everyone}) == len(everyone), "每个提交是一个独立的 Task"
    results = {s["taskId"]: outcome(api, s, timeout=240)["status"] for s in everyone}

    for s in cancelled:
        assert results[s["taskId"]] == "TASK_STATUS_CANCELLED"
        assert fstack.calls(s["taskId"]) == [], "被取消的排队 Task 从未启动"
    for s in [blocker, *readers, *writers[CANCELLED:]]:
        assert results[s["taskId"]] == "TASK_STATUS_DONE"
        assert fstack.count(s["taskId"], "plan_round", "start") == 1, "每个 Task 只启动一次"

    write_intervals = [interval(fstack, s["taskId"]) for s in [blocker, *writers[CANCELLED:]]]
    assert max_overlap(write_intervals) == 1, "写 Task 串行"
    assert max_overlap([interval(fstack, s["taskId"]) for s in readers]) <= READ_ONLY_LIMIT, "只读 Task 并行不超过上限"

    state = asyncio.run(_session_state(fstack.temporal_address, sid))
    assert state.accepted_tasks == len(everyone)
    assert list(state.queued_task_ids) == [] and state.running_write_task_id == "" and list(state.running_read_only_task_ids) == []
    runs = h.fetch(fstack.temporal_address, sid)
    assert len(runs) - 1 >= len(everyone) // CONTINUE_AS_NEW_TASKS - 1, f"Session 应续接多次，实际 {len(runs)} 次运行"
    h.assert_session_clean(runs)
    h.assert_replays_go(fstack.task_service_bin, runs)
