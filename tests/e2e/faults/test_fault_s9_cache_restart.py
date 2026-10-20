"""S-07 场景 9：Cache 重启——SSE 断线续传，以及限流计数丢失后的行为。

设计书 4.5：Cache 只存可丢失或可重建的数据，不能丢的状态放在 DurableEngine 或 RelationalStore。
设计书 4.14：事件带递增 ID，断线后凭 Last-Event-ID 续传。开发环境的 Cache 在 task-service 进程内，
强杀 task-service 再拉起就是 Cache 重启（同时 Session Worker 也重启；业务库与 Temporal 不受影响）。

断言：
- 续传：Cache 里的推流数据丢了，客户端凭 Last-Event-ID 重连，仍能拿到 Task 的结局；补发的事件 ID 都大于
  Last-Event-ID（ID 不回退），task_finished 只有一个。Task 已经结束、或在重启期间结束，都一样。
- 限流：计数丢失后从零开始计（放行，不是全部拒绝），上限照样生效；重复提交的去重不依赖 Cache。
"""

from __future__ import annotations

import uuid

import pytest

from e2e_support import history as h
from e2e_support.api import ApiError, SseStream, TaskApi, finished_payload, stream_until_finished
from e2e_support.scenarios import outcome
from e2e_support.stack import Stack

pytestmark = [pytest.mark.e2e, pytest.mark.faults, pytest.mark.scenario(9)]

RATE_LIMIT = 5
RATE_LIMITED_TENANT = "trl"


def read_until(url: str, token: str, event: str) -> list:
    """读到某类事件为止就断开（模拟客户端断线）。"""
    stream = SseStream(url, token)
    try:
        assert stream.status == 200
        out = []
        for ev in stream:
            out.append(ev)
            if ev.event == event:
                return out
        raise AssertionError(f"推流在 {event} 之前结束：{out}")
    finally:
        stream.close()


def assert_resumed(events: list, last_event_id: int, status: str) -> None:
    assert all(e.id > last_event_id for e in events), f"续传的事件 ID 必须大于 Last-Event-ID {last_event_id}：{[e.id for e in events]}"
    assert [e.event for e in events].count("task_finished") == 1
    assert finished_payload(events)["status"] == status


def test_sse_resume_after_cache_restart_for_a_finished_task(fstack: Stack, api: TaskApi) -> None:
    sub = api.submit()
    events = stream_until_finished(sub["eventsUrl"], api.token or "")
    last_seen = events[0].id  # 客户端只收到了第一个事件就断线

    fstack.restart_task_service()

    resumed = stream_until_finished(sub["eventsUrl"], api.token or "", last_event_id=last_seen, timeout=30)
    assert_resumed(resumed, last_seen, "TASK_STATUS_DONE")
    assert api.query(sub["taskId"])["status"] == "TASK_STATUS_DONE", "Task 的结局在业务库里，不随 Cache 丢失"


def test_sse_resume_after_cache_restart_while_the_task_runs(fstack: Stack, api: TaskApi) -> None:
    sub = api.submit(stub_plan="slow_answer")
    seen = read_until(sub["eventsUrl"], api.token or "", "task_accepted")
    fstack.wait_call(sub["taskId"], "execute_model_node", "start")

    fstack.restart_task_service()
    fstack.open_gate(sub["taskId"])

    resumed = stream_until_finished(sub["eventsUrl"], api.token or "", last_event_id=seen[-1].id, timeout=60)
    assert_resumed(resumed, seen[-1].id, "TASK_STATUS_DONE")
    fresh = stream_until_finished(sub["eventsUrl"], api.token or "", timeout=30)
    assert [e.event for e in fresh].count("task_finished") == 1, "新连上的客户端也拿得到结局"

    # Session Worker 随 task-service 重启：两边的历史都没有异常。
    h.assert_clean(h.analyze(h.fetch_one(fstack.temporal_address, sub["taskId"])))
    runs = h.fetch(fstack.temporal_address, sub["sessionId"])
    h.assert_session_clean(runs, worker_restarted=True)
    h.assert_replays_go(fstack.task_service_bin, runs)


def test_rate_limit_counters_lost_on_cache_restart_restart_from_zero(fstack: Stack) -> None:
    fstack.restart_task_service({"AR_DEV_RATE_LIMIT_PER_MINUTE": str(RATE_LIMIT)})
    limited = TaskApi(fstack.http_url, fstack.token(RATE_LIMITED_TENANT, "u"))
    sub = limited.submit()
    task_id = sub["taskId"]

    def exhaust() -> None:
        for _ in range(RATE_LIMIT):
            limited.query(task_id)  # QueryTask 这条路由的计数
        with pytest.raises(ApiError) as err:
            limited.query(task_id)
        assert err.value.code == "resource_exhausted"

    exhaust()
    fstack.restart_task_service({"AR_DEV_RATE_LIMIT_PER_MINUTE": str(RATE_LIMIT)})
    # 计数丢失：从零开始计，放行；上限照样生效。
    exhaust()

    fstack.restart_task_service(None)
    assert outcome(limited, sub)["status"] == "TASK_STATUS_DONE", "重启前提交的 Task 照常完成"


def test_duplicate_submit_is_still_deduplicated_after_cache_restart(fstack: Stack, api: TaskApi) -> None:
    crid = f"cache-restart-{uuid.uuid4().hex}"
    sub = api.submit(client_request_id=crid, stub_plan="slow_answer")
    fstack.wait_call(sub["taskId"], "execute_model_node", "start")
    fstack.restart_task_service()
    again = api.submit(client_request_id=crid, stub_plan="slow_answer")
    assert again["taskId"] == sub["taskId"] and again.get("deduplicated") is True, "去重不依赖 Cache"
    fstack.open_gate(sub["taskId"])
    assert outcome(api, sub)["status"] == "TASK_STATUS_DONE"
    assert fstack.count(sub["taskId"], "plan_round", "start") == 1
