"""DISK-B（WS-DISK-01 ②，docs/plan/workspace-read-latency-and-disk-2026-11-03.md；租户策略 workspace.cold_after_days，
契约 2026-11-04 人审通过）：挂卷工作区的冷热分层。

1. 闲置超过天数、没有环境占着的工作区：先存一份快照、与环境里的清单核对一致，才删本地目录；状态记成 cold。
2. 下次用到时从快照恢复到本地，内容与转冷前一致，状态回到 active。
3. 最近用过的、还有环境占着的、存档模式的租户都不动。
4. 天数：租户策略的 cold_after_days 优先，没写时用部署的默认值（AR_WORKSPACE_COLD_AFTER_DAYS），都没有时不转冷。
5. 存档失败时本地目录不删，状态回到 active。
6. 转冷进行中借这个工作区：暂时性的错误（workspace_cooling），不在删到一半的目录上执行。
7. cold 的工作区在 Workspace API 里是 ARCHIVED，不是 DELETED。
8. 对账的 activity 带着它跑（最多一小时一次），报告里记转冷了几个。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.errors import TransientError
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.sandbox.ledger import utc_now
from agentruntime_worker.sandbox.reconcile import ReconcileActivities
from agentruntime_worker.workflows.sandbox_reconcile import ReconcileInput
from agentruntime_worker.workspace.cold import cool_idle_workspaces
from agentruntime_worker.workspace.operations import workspace_dict

from .test_workspace_operations import Op, Setup

DAY = timedelta(days=1)
FILES = [{"path": "README.md", "content": "hello\n"}, {"path": "src/app.py", "content": "x = 1\n"}]


async def created(s: Setup, ws: str = "w1") -> Path:
    await s.run(Op.WORKSPACE_OPERATION_CREATE, workspace=ws, files=FILES)
    return s.host / "t1" / ws


async def state(s: Setup, ws: str = "w1") -> str:
    record = await s.archiver.get("t1", ws)
    assert record is not None
    return record.state


def later(days: float) -> Any:
    return utc_now() + timedelta(days=days)


async def test_an_idle_workspace_goes_cold_and_comes_back(tmp_path: Path) -> None:
    s = Setup(tmp_path, mode="mount")
    local = await created(s)
    assert await cool_idle_workspaces(s.broker, default_days=3, now=later(4)) == 1
    assert not local.exists(), "核对过快照才删本地目录"
    assert await state(s) == "cold"
    assert await s.archiver.latest("t1", "w1") is not None
    record = await s.archiver.get("t1", "w1")
    assert workspace_dict(record)["state"] == "WORKSPACE_STATE_ARCHIVED", "cold 的工作区在 API 里是 ARCHIVED"  # type: ignore[arg-type]
    # 下次用到时恢复：内容一样，状态回到 active。
    assert (await s.tool("repo_read", {"path": "README.md"}))["content"] == "hello\n"
    assert (local / "src" / "app.py").read_text() == "x = 1\n"
    assert await state(s) == "active"


async def test_recently_used_or_held_workspaces_stay(tmp_path: Path) -> None:
    s = Setup(tmp_path, mode="mount")
    local = await created(s)
    assert await cool_idle_workspaces(s.broker, default_days=3, now=later(1)) == 0, "没闲置够天数"
    # 挂卷模式下 repo_* 直接读目录、不借环境；shell_run 借的会话级环境留着。
    await s.tool("shell_run", {"argv": ["python", "-c", "print(1)"]})
    assert await cool_idle_workspaces(s.broker, default_days=3, now=later(10)) == 0, "还有环境占着"
    assert local.exists() and await state(s) == "active"


@pytest.mark.parametrize(
    ("policy", "default_days", "idle_days", "cooled"),
    [
        (None, 0, 400, 0),  # 都没配：不转冷
        ({"workspace": {"cold_after_days": 2}}, 0, 3, 1),  # 只有租户策略
        ({"workspace": {"cold_after_days": 30}}, 1, 5, 0),  # 租户策略优先于部署的默认值
        ({"workspace": {"max_workspaces": 5}}, 2, 3, 1),  # 租户没写天数：用部署的默认值
    ],
)
async def test_tenant_policy_overrides_the_deployment_default(
    tmp_path: Path, policy: dict[str, Any] | None, default_days: int, idle_days: int, cooled: int
) -> None:
    s = Setup(tmp_path, mode="mount", policy=policy)
    await created(s)
    assert await cool_idle_workspaces(s.broker, default_days=default_days, now=later(idle_days)) == cooled


async def test_archive_mode_tenants_are_left_alone(tmp_path: Path) -> None:
    s = Setup(tmp_path, mode="archive")
    await created(s)
    assert await cool_idle_workspaces(s.broker, default_days=1, now=later(30)) == 0
    assert await state(s) == "active"


async def test_a_failed_snapshot_keeps_the_local_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Setup(tmp_path, mode="mount")
    local = await created(s)

    async def fail(*_: Any, **__: Any) -> Any:
        raise TransientError("object_store_unavailable", "存不进去")

    monkeypatch.setattr(s.archiver, "snapshot", fail)
    assert await cool_idle_workspaces(s.broker, default_days=3, now=later(4)) == 0
    assert (local / "README.md").read_text() == "hello\n", "存档失败时本地目录不删"
    assert await state(s) == "active"


async def test_leasing_while_cooling_is_a_transient_error(tmp_path: Path) -> None:
    s = Setup(tmp_path, mode="mount")
    await created(s)
    assert await s.archiver.transition("t1", "w1", "active", "cooling")
    with pytest.raises(TransientError) as caught:
        await s.broker.acquire(TenantContext(tenant_id="t1", workspace_id="w1"), SandboxSpec(workspace_id="w1"))
    assert caught.value.code == "workspace_cooling"
    assert not await s.archiver.transition("t1", "w1", "active", "cold"), "状态不对时不转"


async def test_the_reconcile_activity_runs_the_sweep_at_most_hourly(tmp_path: Path) -> None:
    s = Setup(tmp_path, mode="mount")
    await created(s, "w1")
    clock = {"now": later(4)}
    activities = ReconcileActivities(s.broker, cold_after_days=3, clock=lambda: clock["now"])
    report = await activities.reconcile_sandboxes(ReconcileInput())
    assert report.cooled == 1
    await created(s, "w2")  # 按这里的时钟 w2 也闲置够了
    clock["now"] = later(4.01)  # 十几分钟之后：这一轮不扫
    assert (await activities.reconcile_sandboxes(ReconcileInput())).cooled == 0
    assert await state(s, "w2") == "active"
    clock["now"] = later(4.1)  # 过了一小时：再扫
    assert (await activities.reconcile_sandboxes(ReconcileInput())).cooled == 1
    assert await state(s, "w2") == "cold"


def test_the_deployment_default_is_configurable() -> None:
    assert WorkerConfig.from_env({}).workspace_cold_after_days == 0, "不配不转冷"
    assert WorkerConfig.from_env({"AR_WORKSPACE_COLD_AFTER_DAYS": "14"}).workspace_cold_after_days == 14
    for bad in ("-1", "abc", "366"):
        with pytest.raises(ValueError):
            WorkerConfig.from_env({"AR_WORKSPACE_COLD_AFTER_DAYS": bad})
