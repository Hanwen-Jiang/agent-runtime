"""S-03 验收：版本清单写入工作流输入（计划 S-03 交付）；运行中的 Task 绑定启动时的版本，
只有新 Task 使用新版本（设计书 3.6“恢复”、附录 K.4“发布新版本”）。"""

from __future__ import annotations

import pytest

from e2e_support.api import TaskApi, finished_payload, stream_until_finished
from e2e_support.stack import Stack

pytestmark = pytest.mark.e2e

V1 = {"recipe": "stub@v1", "node_templates": {"stub_answer": "v1"}, "model_profiles": {"mock/model": "v1"}, "tenant_policy": "v1"}
V2 = {"recipe": "stub@v2", "node_templates": {"stub_answer": "v2"}, "model_profiles": {"mock/model": "v2"}, "tenant_policy": "v2"}


def pinned(api: TaskApi, task_id: str) -> dict:
    return api.query(task_id)["taskState"]["value"]["pinned"]


def test_running_task_keeps_the_versions_it_started_with(stack: Stack, api: TaskApi) -> None:
    stack.set_pins(V1)
    try:
        old = api.submit(stub_plan="slow_answer")
        stack.wait_call(old["taskId"], "execute_model_node", "start")

        stack.set_pins(V2)  # 发布新版本
        new = api.submit()
        assert finished_payload(stream_until_finished(new["eventsUrl"], api.token or ""))["status"] == "TASK_STATUS_DONE"

        expected_old = {k: v for k, v in V1.items() if k != "tenant_policy"}
        expected_new = {k: v for k, v in V2.items() if k != "tenant_policy"}
        assert pinned(api, old["taskId"]) == expected_old, "运行中的 Task 不受新发布影响"
        assert pinned(api, new["taskId"]) == expected_new, "新 Task 使用新版本"

        stack.open_gate(old["taskId"])
        assert finished_payload(stream_until_finished(old["eventsUrl"], api.token or ""))["status"] == "TASK_STATUS_DONE"
        assert pinned(api, old["taskId"]) == expected_old
    finally:
        stack.set_pins(V1)
