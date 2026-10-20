"""S-07 场景 7：发布新版本时有运行中的 Task，验证版本固定。

设计书 3.6、附录 K.4：运行中的 Task 绑定启动时的代码与模板版本，只有新 Task 使用新版本。
发布 = Registry 发布新的固定版本（改版本文件）+ Worker 滚动重启（杀掉正在执行 Node 的 Worker，拉起新的）。
故障 Worker 的桩 Planner 按 Task 固定的版本选模板版本，所以执行的模板版本能看出 Task 用的是哪一套版本。
"""

from __future__ import annotations

import time

import pytest

from e2e_support import history as h
from e2e_support.api import TaskApi
from e2e_support.scenarios import outcome, vendor_calls
from e2e_support.stack import Stack

pytestmark = [pytest.mark.e2e, pytest.mark.faults, pytest.mark.scenario(7)]

V1 = {"recipe": "stub@v1", "node_templates": {"stub_answer": "v1"}, "model_profiles": {"mock/model": "v1"}, "tenant_policy": "v1"}
V2 = {"recipe": "stub@v2", "node_templates": {"stub_answer": "v2"}, "model_profiles": {"mock/model": "v2"}, "tenant_policy": "v2"}


def planned_with(stack: Stack, task_id: str) -> list[dict]:
    return [c["pinned"] for c in stack.calls(task_id) if c["activity"] == "plan_round" and c["event"] == "pinned"]


def executed_templates(stack: Stack, task_id: str) -> list[str]:
    return [c["template"] for c in vendor_calls(stack, task_id, "end")]


def test_release_with_rolling_worker_restart_keeps_the_running_task_on_its_versions(fstack: Stack, api: TaskApi) -> None:
    fstack.set_pins(V1)
    try:
        old = api.submit(stub_plan="slow_answer")
        fstack.wait_call(old["taskId"], "execute_model_node", "heartbeat")

        # 发布：Registry 换成 V2，Worker 滚动重启（正在执行的 model 节点被打断，在新 Worker 上重试）。
        fstack.set_pins(V2)
        fstack.kill_worker()
        time.sleep(1)
        fstack.start_worker()

        new = api.submit()
        assert outcome(api, new)["status"] == "TASK_STATUS_DONE"
        fstack.open_gate(old["taskId"])
        assert outcome(api, old)["status"] == "TASK_STATUS_DONE"

        assert [p["recipe"] for p in planned_with(fstack, old["taskId"])] == ["stub@v1"], "旧 Task 只规划过一次，用的是启动时的版本"
        assert executed_templates(fstack, old["taskId"]) == ["stub_answer@v1"], "在新 Worker 上重试的 Node 仍用旧模板版本"
        assert [p["recipe"] for p in planned_with(fstack, new["taskId"])] == ["stub@v2"]
        assert executed_templates(fstack, new["taskId"]) == ["stub_answer@v2"], "新 Task 用新版本"

        old_task = h.fetch_one(fstack.temporal_address, old["taskId"])
        new_task = h.fetch_one(fstack.temporal_address, new["taskId"])
        h.assert_clean(h.analyze(old_task), worker_restarted=True)
        h.assert_clean(h.analyze(new_task))
        # 新代码能回放发布前开始的历史。
        h.assert_replays_python([old_task, new_task])
    finally:
        fstack.set_pins(V1)
