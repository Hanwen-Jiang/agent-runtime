"""S-03 验收：人工回复经 Task API 转为 Task 工作流的信号（设计书 3.1.1 第 5 条、附录 K.1）。"""

from __future__ import annotations

import time

import pytest

from e2e_support.api import ApiError, TaskApi, finished_payload, stream_until_finished
from e2e_support.stack import Stack

pytestmark = pytest.mark.e2e


def test_human_reply_resumes_the_task(stack: Stack, api: TaskApi) -> None:
    sub = api.submit(stub_plan="human_gate")
    stack.wait_call(sub["taskId"], "plan_round", "end")
    api.wait_status(sub["taskId"], "TASK_STATUS_RUNNING")
    time.sleep(1)
    assert stack.count(sub["taskId"], "execute_model_node", "start") == 0, "gate 之前不执行后面的节点"

    api.reply(sub["taskId"], "confirm", "approve", "只看华东")
    assert finished_payload(stream_until_finished(sub["eventsUrl"], api.token or ""))["status"] == "TASK_STATUS_DONE"


def test_reply_to_a_queued_task_is_rejected(stack: Stack, api: TaskApi) -> None:
    blocker = api.submit(stub_plan="slow_answer")
    queued = api.submit(session_id=blocker["sessionId"])
    stack.wait_call(blocker["taskId"], "execute_model_node", "start")
    with pytest.raises(ApiError) as err:
        api.reply(queued["taskId"], "confirm", "approve")
    assert err.value.code == "failed_precondition"
    stack.open_gate(blocker["taskId"])
    for sub in (blocker, queued):
        assert finished_payload(stream_until_finished(sub["eventsUrl"], api.token or ""))["status"] == "TASK_STATUS_DONE"
