"""S-03 验收“端到端”：提交 → 桩 Planner → 一个 model 节点（桩模型）→ 桩 Reviewer 判 done → SSE 收到结果。"""

from __future__ import annotations

import pytest

from e2e_support.api import SseStream, TaskApi, finished_payload, stream_until_finished
from e2e_support.stack import Stack

pytestmark = pytest.mark.e2e


def test_submit_streams_the_result(stack: Stack, api: TaskApi) -> None:
    sub = api.submit("用一句话介绍你自己")
    assert sub["taskId"].startswith("tsk_") and sub["sessionId"].startswith("ses_")
    assert sub["eventsUrl"] == f"{stack.http_url}/v1/tasks/{sub['taskId']}/events"

    events = stream_until_finished(sub["eventsUrl"], api.token or "")
    assert [e.event for e in events][0] == "task_accepted"
    assert [e.id for e in events] == list(range(1, len(events) + 1)), "event_id 从 1 开始连续递增"
    result = finished_payload(events)
    assert result["status"] == "TASK_STATUS_DONE"
    assert result["final_verdict"] == "VERDICT_DONE"
    assert result["deliver_node_id"] == "answer"
    assert result["deliverable"]["value"]["text"]

    order = [c["activity"] for c in stack.calls(sub["taskId"]) if c["event"] == "start"]
    assert order == ["plan_round", "execute_model_node", "review_round", "finalize_task"]

    query = api.query(sub["taskId"])
    assert query["status"] == "TASK_STATUS_DONE"
    assert query["sessionId"] == sub["sessionId"]
    assert int(query["lastEventId"]) == events[-1].id


def test_sse_resumes_after_last_event_id(api: TaskApi) -> None:
    sub = api.submit()
    events = stream_until_finished(sub["eventsUrl"], api.token or "")
    resumed = stream_until_finished(sub["eventsUrl"], api.token or "", last_event_id=events[0].id)
    assert [e.id for e in resumed] == [e.id for e in events[1:]], "续传只补发 Last-Event-ID 之后的事件"


def test_sse_requires_authentication(api: TaskApi) -> None:
    sub = api.submit()
    stream = SseStream(sub["eventsUrl"], token=None)
    try:
        assert stream.status == 401
    finally:
        stream.close()
