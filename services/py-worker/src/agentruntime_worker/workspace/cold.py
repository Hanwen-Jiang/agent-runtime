"""挂卷工作区的冷热分层（DISK-B，WS-DISK-01 ②；docs/plan/workspace-read-latency-and-disk-2026-11-03.md）。

挂卷的工作区（<工作区根>/<租户>/<workspace>）闲置超过天数就转冷：先存一份快照（D-06c 的存档），与环境里的清单核对一致，
才删本地目录，状态记成 cold；下次用到时 SandboxBroker 从快照恢复到本地（见 broker._create）。存档成功之前绝不删本地。

- 天数：租户策略的 workspace.cold_after_days 优先，没写时用部署的默认值（AR_WORKSPACE_COLD_AFTER_DAYS），都没有时不转冷。
- 只碰有记录（业务库 workspaces）、状态是 active、挂卷模式的工作区；存档模式的租户本来就不占本地盘。
- 闲置：环境表里这个工作区的环境最后一次用的时间、工作区记录的 updated_at、本地目录与 .git/index 的修改时间，取最晚的。
- 还有环境（creating、ready、destroying）绑着这个工作区时不转冷。
- 状态 active → cooling（抢到的才做；这期间借这个工作区报 workspace_cooling）→ 存档、核对 → cold → 删本地目录。
  任何一步失败退回 active，本地目录不动。
"""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentruntime_core.dev._common import PLATFORM_ID
from agentruntime_core.errors import InvalidError, ProviderError
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.ledger import iso, parse_iso
from agentruntime_worker.workspace.archive import shape

if TYPE_CHECKING:
    from agentruntime_worker.sandbox.broker import SandboxBroker

log = logging.getLogger(__name__)
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
# 转冷时借的环境最多占多久（导出整个工作区）：这期间别人不能回收它。
COOL_BUSY_FOR = timedelta(minutes=30)


def _mtime(path: Path) -> str:
    with contextlib.suppress(OSError):
        return iso(datetime.fromtimestamp(path.stat().st_mtime, tz=UTC))
    return ""


def _parse(stamp: str) -> datetime | None:
    with contextlib.suppress(ValueError):
        return parse_iso(stamp)
    return None


async def _last_used(broker: SandboxBroker, owner: str, workspace_id: str, updated_at: str, local: Path) -> datetime | None:
    assert broker.ledger is not None
    stamps = [await broker.ledger.workspace_last_used(owner, workspace_id), updated_at, _mtime(local), _mtime(local / ".git" / "index")]
    times = [t for t in (_parse(s) for s in stamps if s) if t is not None]
    return max(times) if times else None


async def cool_idle_workspaces(broker: SandboxBroker, *, default_days: int, now: datetime, runtime: str = "python") -> int:
    """扫一遍工作区根，把闲置够天数的挂卷工作区转冷；返回转冷了几个。没有工作区根、存档或环境表时什么都不做。"""
    root = broker.sandboxes.workspace_root
    if root is None or broker.archiver is None or broker.ledger is None or not Path(root).is_dir():
        return 0
    cooled = 0
    for tenant_dir in sorted(p for p in Path(root).iterdir() if p.is_dir() and _SLUG.match(p.name) and p.name != PLATFORM_ID):
        owner = tenant_dir.name
        try:
            policy = await broker.sandboxes.policy_for(owner)
        except ProviderError as err:
            log.warning("读租户 %s 的策略失败（%s），这一轮不转冷", owner, err.code)
            continue
        days = policy.workspace.cold_after_days or default_days
        if days <= 0 or broker.archive_mode(policy.backend):
            continue
        for local in sorted(p for p in tenant_dir.iterdir() if p.is_dir() and _SLUG.match(p.name)):
            try:
                if await _cool_one(broker, owner, local, days=days, now=now, runtime=runtime):
                    cooled += 1
            except ProviderError as err:
                log.warning("工作区 %s 转冷失败（%s），本地目录留着", local.name, err.code)
    return cooled


async def _cool_one(broker: SandboxBroker, owner: str, local: Path, *, days: int, now: datetime, runtime: str) -> bool:
    archiver, ledger = broker.archiver, broker.ledger
    assert archiver is not None and ledger is not None
    ws = local.name
    record = await archiver.get(owner, ws)
    if record is None or record.state != "active":
        return False
    last = await _last_used(broker, owner, ws, record.updated_at, local)
    if last is not None and now - last < timedelta(days=days):
        return False
    if await ledger.workspace_envs(owner, ws) or not await archiver.transition(owner, ws, "active", "cooling"):
        return False
    try:
        # 抢到之后再看一次：抢之前刚建的环境还在就让开。
        if await ledger.workspace_envs(owner, ws):
            raise InvalidError("workspace_in_use", "工作区刚被用上")
        lease = await broker.acquire(
            TenantContext(tenant_id=owner, workspace_id=ws), SandboxSpec(runtime=runtime, workspace_id=ws), writable=False,
            busy_for=COOL_BUSY_FOR, cooling=True,
        )
        try:
            snap = await broker.snapshot(lease)
            stored, live = await archiver.entries(owner, snap), await lease.box.manifest(lease.env_id)  # type: ignore[attr-defined]
            if shape(stored) != shape(live):
                raise InvalidError("workspace_snapshot_mismatch", "快照与本地的工作区对不上")
        finally:
            with contextlib.suppress(ProviderError):
                await broker.release(lease)
    except BaseException:
        with contextlib.suppress(ProviderError):
            await archiver.transition(owner, ws, "cooling", "active")
        raise
    if not await archiver.transition(owner, ws, "cooling", "cold"):
        return False
    # 状态已经是 cold：以快照为准。删不干净的残留在下次恢复前由 broker 清掉。
    with contextlib.suppress(OSError):
        shutil.rmtree(local, onexc=_force_remove)
    log.info("工作区 %s 闲置超过 %d 天，转冷：快照 %s，本地目录已删", ws, days, snap.snapshot_id)
    return True


def _force_remove(function: Any, path: str, _exc: BaseException) -> None:
    """Windows 上 .git 里的只读文件：去掉只读再删（同 workspace.operations）。"""
    with contextlib.suppress(OSError):
        Path(path).chmod(stat.S_IWRITE)
        function(path)


__all__ = ["COOL_BUSY_FOR", "cool_idle_workspaces"]
