"""TSP-2 验收（TS-PROD-01 ②③，docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 2 节）：task-service 多副本。

- 进程内的 Cache 只能单副本（各副本看不到彼此的事件流）：replicas 大于 1 或生产模式时必须配 secrets.valkeyUrl，否则渲染失败。
- 业务库连接池的上限（AR_RELATIONAL_MAX_CONNS）由 taskService.relationalMaxConns 给出；副本数乘它不能超过
  Postgres max_connections（external.postgres.maxConnections）的四分之一，其余留给 Worker、Memory Service、Temporal 与 Langfuse。
- 云上与生产样例都接 Valkey。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from deploy_tests import CHART_DIR, CLOUD_VALUES, PRODUCTION_VALUES, RELEASE, by_kind, env_of, pod_spec, render, require_tool, run


def task_service(manifests: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    [ts] = [m for m in by_kind(manifests, "Deployment") if m["metadata"]["name"].endswith("-task-service")]
    return ts, env_of(pod_spec(ts)["containers"][0])


def render_fails(*set_values: str, values: Sequence[os.PathLike[str]] = ()) -> str:
    helm = require_tool("helm")
    cmd: list[str | os.PathLike[str]] = [helm, "template", RELEASE, CHART_DIR, "--namespace", "agent-runtime"]
    for f in values:
        cmd += ["--values", f]
    for s in set_values:
        cmd += ["--set", s]
    proc = run(cmd, check=False)
    assert proc.returncode != 0, f"应当渲染失败：{set_values}"
    return proc.stderr


def test_more_than_one_replica_needs_valkey() -> None:
    err = render_fails("taskService.replicas=2")
    assert "taskService.replicas" in err and "secrets.valkeyUrl" in err
    ts, _ = task_service(render(set_values=["taskService.replicas=2", "secrets.valkeyUrl.name=agent-runtime-valkey"]))
    assert ts["spec"]["replicas"] == 2


def test_production_needs_valkey() -> None:
    err = render_fails("secrets.valkeyUrl.name=", values=[CLOUD_VALUES])
    assert "secrets.valkeyUrl" in err


def test_cloud_and_production_values_use_valkey() -> None:
    for values in (CLOUD_VALUES, PRODUCTION_VALUES):
        _, env = task_service(render(values))
        assert env["AR_VALKEY_URL"]["valueFrom"]["secretKeyRef"] == {"name": "agent-runtime-valkey", "key": "url"}, values.name


def test_connection_pool_is_rendered_and_budgeted() -> None:
    _, env = task_service(render())
    assert env["AR_RELATIONAL_MAX_CONNS"]["value"] == "8"
    _, env = task_service(render(set_values=["taskService.relationalMaxConns=12"]))
    assert env["AR_RELATIONAL_MAX_CONNS"]["value"] == "12"
    # 200 / 4 = 50：7 × 8 = 56 超了。
    err = render_fails("taskService.replicas=7", "secrets.valkeyUrl.name=agent-runtime-valkey")
    assert "taskService.relationalMaxConns" in err and "external.postgres.maxConnections" in err
    for values in (CLOUD_VALUES, PRODUCTION_VALUES):
        render(values)
