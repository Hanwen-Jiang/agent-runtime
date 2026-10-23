"""T-11b 验收（端到端）：进度事件从 Python 的 Task 工作流，经跨语言 activity publish_task_event 交给 Go 的 Session Worker，
由业务库分配 event_id、推进 Cache，客户端从 SSE 收到（设计书 4.14、附录 K.1；R-05 评审 D10）。

- 事件依次是 task_accepted、round_started、stage_started、（human_input_required）、node_finished……、verdict、task_finished，
  event_id 从 1 起连续递增；
- 客户端凭 human_input_required 里的 question_id 与选项发人工回复，Task 接着跑完；
- 断线后凭 Last-Event-ID 续传，进度事件一样补得回来。
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from e2e_support.api import SseEvent, SseStream, TaskApi, finished_payload, stream_until_finished
from e2e_support.stack import Stack

pytestmark = pytest.mark.e2e

DEADLINE_S = 60


def payload(ev: SseEvent) -> dict[str, Any]:
    return ev.data["payload"]["value"]


def test_a_plain_task_streams_its_progress(stack: Stack, api: TaskApi) -> None:
    sub = api.submit(stub_plan="answer")
    events = stream_until_finished(sub["eventsUrl"], api.token or "")
    assert [e.event for e in events] == ["task_accepted", "round_started", "stage_started", "node_finished", "verdict", "task_finished"]
    assert [e.id for e in events] == list(range(1, len(events) + 1)), "event_id 从 1 起连续递增"
    started, finished, verdict = payload(events[2]), payload(events[3]), payload(events[4])
    assert started["event"] == "stage_started" and [n["id"] for n in started["nodes"]] == ["answer"]
    assert (finished["node_id"], finished["status"]) == ("answer", "succeeded")
    assert verdict["verdict"] == "done"
    assert finished_payload(events)["status"] == "TASK_STATUS_DONE"


def test_the_client_answers_the_question_it_sees_on_the_stream(stack: Stack, api: TaskApi) -> None:
    sub = api.submit(stub_plan="human_gate")
    stream = SseStream(sub["eventsUrl"], api.token, timeout=90)
    # 心跳让连接一直不超时：问题一直不来时由这个定时器断开推流，测试失败而不是一直等下去。
    deadline = threading.Timer(DEADLINE_S, stream.close)
    deadline.start()
    seen: list[SseEvent] = []
    try:
        assert stream.status == 200
        for ev in stream:
            seen.append(ev)
            if ev.event == "human_input_required":
                question = payload(ev)
                assert question["question_id"] == "confirm" and question["kind"] == "human" and "approve" in question["options"]
                api.reply(sub["taskId"], question["question_id"], "approve")
            if ev.event == "task_finished":
                break
    except (OSError, ValueError, AttributeError):
        pass  # 定时器关掉了连接
    finally:
        deadline.cancel()
        stream.close()

    names = [e.event for e in seen]
    assert "human_input_required" in names, f"{DEADLINE_S} 秒内推流里没有等待人工回复的事件：{names}"
    assert names == [
        "task_accepted", "round_started", "stage_started", "human_input_required", "node_finished", "node_finished", "verdict", "task_finished",
    ]
    assert [e.id for e in seen] == list(range(1, len(seen) + 1))
    assert finished_payload(seen)["status"] == "TASK_STATUS_DONE"

    # 断线后续传：从 human_input_required 之后接着收，进度事件一个不少。
    resumed = stream_until_finished(sub["eventsUrl"], api.token or "", last_event_id=seen[3].id)
    assert [e.event for e in resumed] == names[4:]
    assert int(api.query(sub["taskId"])["lastEventId"]) == seen[-1].id
