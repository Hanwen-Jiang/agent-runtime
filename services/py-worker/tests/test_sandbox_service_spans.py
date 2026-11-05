"""SBX-01：Worker 记的 runtime.sandbox span 与 shell_run 的结果，取自 Sandbox Service 给的借用（Lease）。

借还、回退、计费都在服务里（services/go-sandbox-service 的测试）；这里看 Worker 一侧把服务给的东西原样记下：

1. 一次性的环境用完销毁：span 里有环境号、后端、没复用，花费与环境表里的一致；结果里有花费，环境表记着 Task。
2. 回退（D-12）：Lease 上的 fallback_from 与原因进 span。
3. 存档模式（D-06）：span 标着 workspace.mode 与恢复的快照。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentruntime.environment.v1 import environment_pb2
from agentruntime.sandbox.v1 import sandbox_pb2 as pb
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.client import RemoteBox, RemoteLease

from .service_setup import Setup


async def test_shell_run_records_cost_env_and_task_in_the_span(tmp_path: Path) -> None:
    s = Setup(tmp_path, specs={"local": {"kind": "local", "isolation": "none", "price": {"per_hour": 3.6}}}, sessions=False)
    out = await s.call("shell_run", {"argv": ["python", "-c", "import time; time.sleep(0.3); print(1)"]}, session="", task_id="task-77")
    assert out["exit_code"] == 0 and out["sandbox"] == "local"
    [span] = s.spans()
    row = next(r for r in s.envs("t1", active=False) if r["env_ref"] == span["sandbox.env_ref"])
    assert row["state"] == "destroyed" and row["task_id"] == "task-77"
    assert row["cost_usd"] > 0 and span["sandbox.cost_usd"] == pytest.approx(row["cost_usd"])
    assert out["sandbox_cost_usd"] == pytest.approx(row["cost_usd"]), "Node Executor 把它计入 Node 的花费"
    assert span["sandbox.backend"] == "local" and span["sandbox.reused"] is False


def lease(**fields: object) -> RemoteLease:
    msg = pb.Lease(lease_id="sbe-0123456789abcdef", backend=pb.Backend(name="cloudflare", kind="cloudflare", isolation="microvm"),
                   record=environment_pb2.EnvRecord(env_ref="sbe-0123456789abcdef", workspace_id="w1"), **fields)  # type: ignore[arg-type]
    return RemoteLease.of(msg, RemoteBox(None, TenantContext(tenant_id="t1", workspace_id="w1")))  # type: ignore[arg-type]


def test_fallback_goes_into_the_span() -> None:
    attrs = lease(fallback_from="kata", fallback_reason="kata：transient: 起不来").attributes
    assert attrs["sandbox.backend"] == "cloudflare" and attrs["sandbox.isolation"] == "microvm"
    assert attrs["sandbox.fallback_from"] == "kata" and attrs["sandbox.fallback_reason"].startswith("kata：")
    assert "sandbox.fallback_from" not in lease().attributes


def test_archive_mode_and_the_restored_snapshot_go_into_the_span() -> None:
    attrs = lease(archive=True, restored_snapshot="snap-1").attributes
    assert attrs["workspace.mode"] == "archive" and attrs["workspace.restored_snapshot"] == "snap-1"
    assert attrs["sandbox.service"] is True
