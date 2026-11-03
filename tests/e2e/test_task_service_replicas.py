"""TSP-2 验收（TS-PROD-01 ③，docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 2 节）：task-service 两个副本。

两个副本共用一个 Valkey（CACHE-01）与同一个业务库：连 A 提交、连 B 收事件；A 在 Task 进行中被强杀后，客户端带
Last-Event-ID 改连 B 续传，事件不丢、不重、id 连续，Task 正常结束。Session 工作流的 activity 落在哪个副本上不定，
这也覆盖了“A 写进流、B 推给客户端”。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from e2e_support.api import SseStream, TaskApi, finished_payload, stream_until_finished
from e2e_support.replicas import ReplicaStack

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def replicas(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ReplicaStack]:
    s = ReplicaStack(root=tmp_path_factory.mktemp("replicas"))
    try:
        s.start()
    except Exception as exc:  # noqa: BLE001 — 起不来时附上各进程的输出
        diagnostics = s.diagnostics()
        s.stop()
        pytest.fail(f"两副本的环境没有起来：{exc}\n{diagnostics}", pytrace=False)
    yield s
    s.stop()


def events_url(replicas: ReplicaStack, replica: str, task_id: str) -> str:
    return f"{replicas.url(replica)}/v1/tasks/{task_id}/events"


def test_submit_on_a_stream_from_b(replicas: ReplicaStack) -> None:
    token = replicas.token("t1", "u1")
    a, b = TaskApi(replicas.url("a"), token), TaskApi(replicas.url("b"), token)
    sub = a.submit("用一句话介绍你自己")
    events = stream_until_finished(events_url(replicas, "b", sub["taskId"]), token)
    assert events[0].event == "task_accepted"
    assert [e.id for e in events] == list(range(1, len(events) + 1)), "B 推送的事件 id 从 1 开始连续"
    assert finished_payload(events)["status"] == "TASK_STATUS_DONE"
    assert b.query(sub["taskId"])["status"] == "TASK_STATUS_DONE", "B 也能查到 A 提交的 Task"


def test_resume_on_b_after_a_dies(replicas: ReplicaStack) -> None:
    token = replicas.token("t1", "u2")
    a = TaskApi(replicas.url("a"), token)
    sub = a.submit(stub_plan="slow_answer")
    replicas.wait_call(sub["taskId"], "execute_model_node", "start")

    stream = SseStream(events_url(replicas, "a", sub["taskId"]), token)
    assert stream.status == 200
    seen = []
    for ev in stream:
        seen.append(ev)
        break  # 至少收到 task_accepted 之后再杀 A
    replicas.kill_replica("a")
    stream.close()

    replicas.open_gate(sub["taskId"])
    rest = stream_until_finished(events_url(replicas, "b", sub["taskId"]), token, last_event_id=seen[-1].id, timeout=120)
    ids = [e.id for e in seen + rest]
    assert ids == list(range(1, len(ids) + 1)), f"续传不丢不重：{ids}"
    assert finished_payload(rest)["status"] == "TASK_STATUS_DONE"
