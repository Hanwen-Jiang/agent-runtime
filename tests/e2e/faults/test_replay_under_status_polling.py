"""S-04 缺陷修复的夜间回归（D5）：human 节点的 Hook 运行时客户端轮询 QueryTask，事件历史仍能回放。

修复之前，human 节点前的 Hook（local activity）运行时来了查询，工作流任务会在 local activity 的 marker 之后切开，
s05-human-channel 的标记落进下一个工作流任务，这样的历史用任何代码都回放不了（本机重复 40 次约四分之一）。
这里重复 REPEAT 次：提交 human_gate，从提交起另一个线程不停地查询它的状态；从推流里看到问题就回复，等 Task 完成；
最后逐份回放。修复之前几乎必然失败（20 次都碰不上的概率不到千分之五）。只进夜间任务（faults 标记）。
"""

from __future__ import annotations

import threading

import pytest

from e2e_support import history as h
from e2e_support.api import SseEvent, SseStream, TaskApi, finished_payload
from e2e_support.stack import Stack

pytestmark = [pytest.mark.e2e, pytest.mark.faults]

REPEAT = 20
DEADLINE_S = 60
POLL_INTERVAL_S = 0.01


def poll_status(api: TaskApi, task_id: str, stop: threading.Event) -> None:
    """不停地查询 Task 的状态（QueryTask 会对 Task 工作流发 Temporal 查询），直到 stop。结果不看。"""
    while not stop.is_set():
        try:
            api.query(task_id)
        except Exception:  # noqa: BLE001 — 只为制造查询
            pass
        stop.wait(POLL_INTERVAL_S)


def answer_while_polling(stack: Stack, api: TaskApi) -> str:
    sub = api.submit(stub_plan="human_gate")
    stop = threading.Event()
    poller = threading.Thread(target=poll_status, args=(TaskApi(stack.http_url, stack.token("t1", "u1")), sub["taskId"], stop), daemon=True)
    poller.start()
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
                stop.set()
                api.reply(sub["taskId"], ev.data["payload"]["value"]["question_id"], "approve")
            if ev.event == "task_finished":
                break
    except (OSError, ValueError, AttributeError):
        pass  # 定时器关掉了连接
    finally:
        stop.set()
        deadline.cancel()
        stream.close()
        poller.join(5)
    names = [e.event for e in seen]
    assert "human_input_required" in names and names[-1:] == ["task_finished"], f"{sub['taskId']} 没有问完就结束：{names}"
    assert finished_payload(seen)["status"] == "TASK_STATUS_DONE"
    return str(sub["taskId"])


def test_histories_replay_when_the_status_is_polled_during_hooks(stack: Stack, api: TaskApi) -> None:
    task_ids = [answer_while_polling(stack, api) for _ in range(REPEAT)]
    histories = [h.fetch_one(stack.temporal_address, task_id) for task_id in task_ids]
    h.assert_replays_python(histories, env=stack.shared_store_env())
