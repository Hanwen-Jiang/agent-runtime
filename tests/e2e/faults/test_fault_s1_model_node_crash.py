"""S-07 场景 1：Worker 在 model 节点执行中崩溃。

设计书 3.6“恢复”、附录 K.4：activity 超时（心跳）后由其他 Worker 重试；effect 为 none 的 Node 直接重跑，
已完成的步骤（Planner）不重做。连续崩溃两次也一样。
"""

from __future__ import annotations

import time

import pytest

from e2e_support import history as h
from e2e_support.api import TaskApi
from e2e_support.scenarios import outcome
from e2e_support.stack import Stack, wait_until

pytestmark = [pytest.mark.e2e, pytest.mark.faults, pytest.mark.scenario(1)]


@pytest.mark.parametrize("kills", [1, 2])
def test_worker_killed_during_model_node_task_finishes_without_redoing_steps(fstack: Stack, api: TaskApi, kills: int) -> None:
    sub = api.submit(stub_plan="slow_answer")
    task_id = sub["taskId"]
    for n in range(1, kills + 1):
        # 第 n 次 attempt 开始等门（桩模型记一次 heartbeat）后杀掉 Worker。
        wait_until(
            lambda n=n: any(c["attempt"] == n for c in fstack.calls(task_id) if c["activity"] == "execute_model_node" and c["event"] == "heartbeat"),
            f"model 节点第 {n} 次 attempt 在执行中",
        )
        fstack.kill_worker()
        time.sleep(1)
        fstack.start_worker()
    fstack.open_gate(task_id)

    assert outcome(api, sub)["status"] == "TASK_STATUS_DONE"

    # 外部副作用：model 节点只有最后一次 attempt 完成；Planner、Reviewer、收尾各一次。没有写操作。
    assert fstack.count(task_id, "plan_round", "end") == 1
    assert fstack.count(task_id, "review_round", "end") == 1
    fstack.wait_finalized(task_id)
    assert fstack.count(task_id, "finalize_task", "end") == 1
    assert fstack.count(task_id, "execute_model_node", "end") == 1
    assert fstack.count(task_id, "execute_model_node", "start") == kills + 1

    # 事件历史：model 节点只调度一次，在同一次调度里重试到第 kills + 1 次 attempt。
    task = h.fetch_one(fstack.temporal_address, task_id)
    report = h.analyze(task)
    h.assert_clean(report, worker_restarted=True)
    node = report.activity("execute_model_node", "answer")
    assert node.outcome == "completed" and node.attempts == kills + 1
    h.assert_replays_python([task])
    h.assert_replays_go(fstack.task_service_bin, h.fetch(fstack.temporal_address, sub["sessionId"]))
