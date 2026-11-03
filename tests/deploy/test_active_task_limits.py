"""task-service 准入时未终态 Task 的上限按租户覆盖（AR_TENANT_ACTIVE_TASKS）：默认每个租户 2 个（开放注册，docs/plan/open-signup.md），
内部租户（评测、跑分）在 values 的 taskService.activeTaskLimits 里放开。不给时不渲染，与以前一样。
"""

from __future__ import annotations

from typing import Any

from deploy_tests import CLOUD_VALUES, by_kind, env_of, pod_spec, render


def task_service(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    [ts] = [m for m in by_kind(manifests, "Deployment") if m["metadata"]["name"].endswith("-task-service")]
    return env_of(pod_spec(ts)["containers"][0])


def test_off_by_default() -> None:
    for manifests in (render(), render(CLOUD_VALUES)):
        assert "AR_TENANT_ACTIVE_TASKS" not in task_service(manifests)


def test_limits_are_rendered_sorted_by_tenant() -> None:
    env = task_service(render(CLOUD_VALUES, set_values=["taskService.activeTaskLimits.swebench=6", "taskService.activeTaskLimits.evals=0"]))
    assert env["AR_TENANT_ACTIVE_TASKS"]["value"] == "evals=0,swebench=6"
