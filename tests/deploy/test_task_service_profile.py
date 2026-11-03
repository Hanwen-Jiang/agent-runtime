"""TSP-1 验收（TS-PROD-01 ①，docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 2 节）：task-service 的生产模式。

values 的 taskService.profile 渲染成 AR_PROFILE。生产模式下 task-service 没有 AR_RELATIONAL_DSN 拒绝启动、不回落 SQLite，
所以 Chart 在 profile 是 production 而 secrets.relationalDsn 没配时直接渲染失败，不等 Pod 起来再崩。默认是 development，
云上与生产样例是 production。
"""

from __future__ import annotations

import os
from typing import Any

from deploy_tests import CHART_DIR, CLOUD_VALUES, PRODUCTION_VALUES, RELEASE, by_kind, env_of, pod_spec, render, require_tool, run


def task_service(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    [ts] = [m for m in by_kind(manifests, "Deployment") if m["metadata"]["name"].endswith("-task-service")]
    return env_of(pod_spec(ts)["containers"][0])


def test_development_by_default() -> None:
    assert task_service(render())["AR_PROFILE"]["value"] == "development"


def test_cloud_and_production_values_run_in_production_mode() -> None:
    for values in (CLOUD_VALUES, PRODUCTION_VALUES):
        env = task_service(render(values))
        assert env["AR_PROFILE"]["value"] == "production", values.name
        assert "AR_RELATIONAL_DSN" in env, values.name


def test_production_without_a_dsn_fails_to_render() -> None:
    helm = require_tool("helm")
    # 默认 values 的 secrets.relationalDsn.name 为空，出网网关与包镜像也没开（它们各自也要连接串），失败只能来自 task-service。
    cmd: list[str | os.PathLike[str]] = [
        helm, "template", RELEASE, CHART_DIR, "--namespace", "agent-runtime", "--set", "taskService.profile=production",
    ]
    proc = run(cmd, check=False)
    assert proc.returncode != 0
    assert "taskService.profile" in proc.stderr and "secrets.relationalDsn" in proc.stderr


def test_unknown_profile_fails_to_render() -> None:
    helm = require_tool("helm")
    proc = run([helm, "template", RELEASE, CHART_DIR, "--namespace", "agent-runtime", "--set", "taskService.profile=prod"], check=False)
    assert proc.returncode != 0
    assert "taskService.profile" in proc.stderr
