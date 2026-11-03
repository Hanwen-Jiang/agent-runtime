"""DISK-A 验收（WS-DISK-01 ①③，docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 2 节）：挂卷的工作区也有大小上限、卷用量告警。

挂卷模式不存档，原来没有任何大小限制（第 1.4 节）。这里要的：
1. 写类工具（repo_edit、shell_run）跑完量一次工作区（不跟随符号链接）；超过租户策略的 workspace.max_bytes 时这次的结果里带上
   workspace_size（字节数、上限、提示），之后的写类工具一律拒绝（denied，workspace_too_large），说明大小、上限与怎么清理。
2. 清理之后（例如经 Workspace API 丢弃改动）再写，先重新量，量下来不超就放行。
3. 量大小有开销：没超时两次测量至少隔 workspace_size_interval_s；超了之后每次写都重新量（等清理）。
4. 存档模式不在这里管（存档时已核对 max_bytes）。
5. 卷用量：对账每一轮报一次工作区卷的已用比例（runtime.workspace.volume_used_ratio），也记在对账的报告里。
"""

from __future__ import annotations

import io
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.errors import DeniedError
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.broker import SandboxBroker
from agentruntime_worker.sandbox.ledger import SandboxLedger
from agentruntime_worker.sandbox.reconcile import ReconcileActivities
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.tools.sandboxes import Sandboxes
from agentruntime_worker.workflows.sandbox_reconcile import ReconcileInput

LIMIT = 4096


class Setup:
    def __init__(self, tmp_path: Path, *, max_bytes: int = LIMIT, interval: float = 0, shell: str = "sandbox",
                 specs: Mapping[str, Any] | None = None) -> None:
        self.workspaces = tmp_path / "workspaces"
        (self.workspaces / "t1" / "w1").mkdir(parents=True, exist_ok=True)
        policy = {"workspace": {"max_bytes": max_bytes}}

        async def lookup(tenant_id: str) -> Mapping[str, Any] | None:
            return policy

        self.boxes = Sandboxes(dict(specs or {"local": {"kind": "local", "isolation": "none"}}), "local", workspace_root=self.workspaces, policy=lookup)
        self.broker = SandboxBroker(self.boxes, SandboxLedger(SQLiteRelationalStoreProvider(tmp_path / "relational")))
        self.tools = LocalToolDriverProvider()
        register_platform_tools(
            self.tools,
            PlatformToolsConfig(workspace_root=self.workspaces, shell_sandbox=shell, workspace_size_interval_s=interval, workspace_max_bytes=max_bytes),
            sandboxes=self.broker,
        )

    @property
    def root(self) -> Path:
        return self.workspaces / "t1" / "w1"

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        tenant = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1")
        return (await self.tools.bind(tenant).invoke(tool, "v1", args, idempotency_key=f"idk-{os.urandom(4).hex()}")).output


async def test_crossing_the_limit_warns_then_refuses_writes(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    small = await s.call("repo_edit", {"path": "a.txt", "new": "x" * 100})
    assert "workspace_size" not in small
    big = await s.call("repo_edit", {"path": "big.txt", "new": "y" * (LIMIT + 1)})
    warning = big["workspace_size"]
    assert warning["over_limit"] is True and warning["max_bytes"] == LIMIT and warning["bytes"] > LIMIT
    assert "清理" in warning["message"]
    for tool, args in (("repo_edit", {"path": "c.txt", "new": "z"}), ("shell_run", {"argv": ["python", "-c", "print(1)"]})):
        with pytest.raises(DeniedError) as info:
            await s.call(tool, args)
        assert info.value.code == "workspace_too_large"
        assert str(LIMIT) in info.value.message and "清理" in info.value.message
    assert not (s.root / "c.txt").exists(), "被拒的写没有落盘"
    # 读类工具照常可用。
    assert (await s.call("repo_read", {"path": "a.txt"}))["content"]


async def test_cleanup_lets_writes_resume(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    await s.call("repo_edit", {"path": "big.txt", "new": "y" * (LIMIT + 1)})
    with pytest.raises(DeniedError):
        await s.call("repo_edit", {"path": "c.txt", "new": "z"})
    (s.root / "big.txt").unlink()  # 例如经 Workspace API 丢弃改动
    out = await s.call("repo_edit", {"path": "c.txt", "new": "z"})
    assert out["created"] and "workspace_size" not in out


async def test_measurement_is_throttled_while_under_the_limit(tmp_path: Path) -> None:
    s = Setup(tmp_path, interval=3600)
    await s.call("repo_edit", {"path": "a.txt", "new": "x"})  # 第一次量：没超
    (s.root / "sneaky.bin").write_bytes(b"0" * (LIMIT * 2))  # 两次测量之间长大
    out = await s.call("repo_edit", {"path": "b.txt", "new": "x"})
    assert "workspace_size" not in out, "间隔内不重新量"


async def test_host_shell_mode_uses_the_configured_limit(tmp_path: Path) -> None:
    workspaces = tmp_path / "workspaces"
    (workspaces / "t1" / "w1").mkdir(parents=True)
    tools = LocalToolDriverProvider()
    register_platform_tools(tools, PlatformToolsConfig(workspace_root=workspaces, workspace_size_interval_s=0, workspace_max_bytes=LIMIT))
    tenant = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1")
    bound = tools.bind(tenant)
    out = (await bound.invoke("repo_edit", "v1", {"path": "big.txt", "new": "y" * (LIMIT + 1)}, idempotency_key="idk-1")).output
    assert out["workspace_size"]["over_limit"] is True
    with pytest.raises(DeniedError):
        await bound.invoke("shell_run", "v1", {"argv": ["python", "-c", "print(1)"]}, idempotency_key="idk-2")


async def test_symlinks_are_not_followed(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"0" * (LIMIT * 4))
    try:
        (s.root / "link").symlink_to(outside)
    except OSError:
        pytest.skip("这台机器上建不了符号链接")
    out = await s.call("repo_edit", {"path": "a.txt", "new": "x"})
    assert "workspace_size" not in out, "链接指向的大文件不算进工作区"


async def test_reconcile_reports_the_volume_usage(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
    report = await ReconcileActivities(s.broker, telemetry=telemetry).reconcile_sandboxes(ReconcileInput())
    assert report.volume_used_ratio is not None and 0 < report.volume_used_ratio < 1
    await telemetry.bind_platform("sandbox-reconcile").flush()
    [metric] = [m for m in telemetry.exported_metrics if m.name == "runtime.workspace.volume_used_ratio"]
    assert metric.value == pytest.approx(report.volume_used_ratio)
    assert metric.attributes.get("volume") == "workspaces"
