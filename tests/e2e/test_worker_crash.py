"""S-03 验收“杀 Worker”：在 Stage 执行中杀掉 Python Worker，重启后 Task 完成；
用调用计数断言已完成的 activity 不重跑（设计书 3.6“恢复”、附录 K.4）。"""

from __future__ import annotations

import time

import pytest

from e2e_support.api import TaskApi, finished_payload, stream_until_finished
from e2e_support.stack import Stack

pytestmark = pytest.mark.e2e


def test_task_completes_after_the_worker_is_killed_mid_stage(stack: Stack, api: TaskApi) -> None:
    sub = api.submit(stub_plan="slow_answer")
    task_id = sub["taskId"]
    first = stack.wait_call(task_id, "execute_model_node", "start")
    stack.wait_call(task_id, "execute_model_node", "heartbeat")

    stack.kill_worker()
    time.sleep(1)
    stack.start_worker()
    stack.open_gate(task_id)

    result = finished_payload(stream_until_finished(sub["eventsUrl"], api.token or "", timeout=120))
    assert result["status"] == "TASK_STATUS_DONE"

    # 杀掉之前已完成的 activity 不重跑；被打断的 model 节点在新 Worker 上重试一次后完成。
    assert stack.count(task_id, "plan_round", "start") == 1
    assert stack.count(task_id, "plan_round", "end") == 1
    assert stack.count(task_id, "execute_model_node", "end") == 1
    assert stack.count(task_id, "review_round", "end") == 1
    assert stack.count(task_id, "finalize_task", "end") == 1
    # Windows 上 venv 的 python.exe 是启动器，Popen 拿到的 pid 不是解释器的，所以只和被杀的那次比。
    ends = [c for c in stack.calls(task_id) if c["activity"] == "execute_model_node" and c["event"] == "end"]
    assert ends[0]["pid"] != first["pid"], "model 节点应在重启后的 Worker 上完成"
    assert ends[0]["attempt"] >= 2, "被打断的 attempt 超过 heartbeat 超时后重试"
