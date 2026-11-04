"""沙箱环境表的对账（W9；D-11；docs/plan/sandbox-platform.md 第 1 节）。

工作流在 workflows/sandbox_reconcile.py（平台级、固定的工作流 ID，每隔 interval_s 跑一次 reconcile_sandboxes activity）；
这里是 activity 与它做的事。

一轮做的事（reconcile_once）：
1. 列出每个能列的后端在跑的环境（列不了的后端这一轮跳过，不据此判丢失）。
2. 平台目录里没结束的环境，逐个绑定到所属租户读它的行：
   - creating 超过宽限还没就绪：记为 failed（后端要是已经建了，下面当孤儿删掉）；
   - ready 而后端的列表里没有：记为丢失（不在后端重建）；
   - ready 且空闲到期或活满、没有命令在跑：销毁（会话级沙箱的回收，W5）；存档模式的可写环境先存档（D-06）。
     例外（docs/plan/feedback-2026-11-01.md N-05）：会话级的环境空闲到期、而它最近服务的 Task 还没结束（多半在等人回复）时顺延一个
     空闲期，从最后一次使用算起最多 hold_max，也不超过活满；

   - destroying：再删一次，删掉了（或后端里已经没了）记为 destroyed。
   目录里有、租户的行没有（写了目录、没写成行就中断了）：超过宽限把目录记为 failed。
3. 后端列出来的环境，环境表里没有或已经结束的：超过宽限（按后端记的创建时间）就是孤儿，销毁。只碰本部署建的环境
   （K8s 按标签、桥按令牌），作用域从后端记的 owner 来，不猜。
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from temporalio import activity

from agentruntime_core.dev._common import PLATFORM_ID
from agentruntime_core.errors import NotFoundError, ProviderError
from agentruntime_core.providers.sandbox import ListedEnvironment
from agentruntime_core.providers.telemetry import TelemetryProvider
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker import names
from agentruntime_worker.sandbox.ledger import iso, parse_iso, utc_now
from agentruntime_worker.workflows.sandbox_reconcile import ReconcileInput, ReconcileReport
from agentruntime_worker.workspace.cold import cool_idle_workspaces

if TYPE_CHECKING:
    from agentruntime_worker.sandbox.broker import SandboxBroker

log = logging.getLogger(__name__)
_OWNER = re.compile(r"^(_platform|[a-z0-9][a-z0-9_-]{0,62})$")


DEFAULT_HOLD_MAX = timedelta(hours=2)


def hold_until(record: Any, now: datetime, hold_max: timedelta) -> datetime | None:
    """等人期间顺延到什么时候：再给一个空闲期，从最后一次使用算起最多 hold_max，也不超过活满；已经到头时为 None。"""
    last = parse_iso(record.last_used_at) or parse_iso(record.ready_at)
    if last is None or not record.session_id:
        return None
    candidates = [now + timedelta(seconds=int(record.idle_seconds or 0)), last + hold_max]
    end = parse_iso(record.max_until)
    if end is not None:
        candidates.append(end)
    until = min(candidates)
    return until if until > now else None


async def reconcile_once(broker: SandboxBroker, *, grace: timedelta = timedelta(minutes=10), hold_max: timedelta = DEFAULT_HOLD_MAX) -> ReconcileReport:
    report = ReconcileReport()
    ledger = broker.ledger
    if ledger is None:
        return report
    sandboxes = broker.sandboxes
    now = ledger.clock()
    listings: dict[str, dict[str, ListedEnvironment]] = {}
    for name in sandboxes.names:
        if not sandboxes.buildable(name):
            continue  # 还没有实现的种类（firecracker，D-20）只能登记为停用，里面不会有环境
        # 停用的后端（D-19）照常列出：排空中的环境要照常判丢失、回收，孤儿照常删。
        try:
            backend = await sandboxes.backend(name)
        except ProviderError as err:
            report.skipped_backends.append(f"{name}：{err.code}")
            continue
        lister = getattr(backend.provider, "list_environments", None)
        if lister is None:
            report.skipped_backends.append(f"{name}：不能列出")
            continue
        try:
            listings[name] = {e.env_id: e for e in await lister()}
        except ProviderError as err:
            report.skipped_backends.append(f"{name}：{err.code}")
    seen: dict[str, tuple[str, ListedEnvironment]] = {}
    for name, items in listings.items():
        for env_id, item in items.items():
            seen.setdefault(env_id, (name, item))
    report.listed = len(seen)

    # 2. 目录里没结束的环境。
    handled: set[str] = set()
    for entry in await ledger.directory(active_only=True):
        try:
            record = await ledger.get(entry.owner, entry.env_ref)
            if record is None:
                stale = parse_iso(entry.updated_at)
                if stale is not None and now - stale > grace:
                    await ledger._directory(_Stub(entry.env_ref, entry.owner, entry.backend), "failed")  # noqa: SLF001
                    report.stale_failed += 1
                continue
            if record.backend_env_id:
                handled.add(record.backend_env_id)
            listing = listings.get(record.backend)
            if record.state == "creating":
                created = parse_iso(record.created_at)
                if created is not None and now - created > grace:
                    await ledger.mark_failed(record, "creating 超过宽限还没就绪（Worker 中途退出？）")
                    report.stale_failed += 1
            elif record.state == "ready":
                if listing is not None and record.backend_env_id not in listing:
                    await ledger.mark_lost(record, "后端的列表里没有这个环境")
                    report.lost += 1
                elif record.expired(now) and ledger.idle_now(record):
                    # 存档模式的可写环境先存档（D-06）：存档暂时失败时留着、下一轮再试；活满了照样销毁。
                    lifetime = bool(record.max_until and record.max_until <= iso(now))
                    until = None if lifetime else hold_until(record, now, hold_max)
                    if until is not None and await ledger.task_running(record.owner, record.task_id):
                        await ledger.hold(record, until)
                        report.held += 1
                        continue
                    reason = "max lifetime" if lifetime else "idle expired"
                    await broker._destroy_record(record, reason, force=lifetime)  # noqa: SLF001
                    report.reaped += 1
            elif record.state == "destroying":
                await broker._destroy_record(record, record.reason or "retry destroy")  # noqa: SLF001
                report.destroy_retried += 1
        except ProviderError as err:
            report.errors.append(f"{entry.env_ref}：{err.code}")
            log.warning("对账 %s 失败（%s）", entry.env_ref, err.code)

    # 3. 后端里有、环境表里没有（或已经结束）的：孤儿。
    for env_id, (name, item) in sorted(seen.items()):
        if env_id in handled:
            continue
        owner = item.owner
        if not _OWNER.match(owner or ""):
            report.errors.append(f"{env_id}：后端记的作用域格式不对，不碰")
            continue
        try:
            record = await ledger.by_backend_env(owner, None, env_id)
            if record is not None and record.state in ("ready", "creating", "destroying"):
                continue  # 目录里漏了它也照样活着：下一轮目录会补上，不当孤儿
            if record is None and (item.created_at is None or now - item.created_at <= grace):
                continue  # 刚建的，可能还没来得及记进环境表
            backend = await sandboxes.backend(name)
            box = backend.provider.bind_platform("sandbox-reconcile") if owner == PLATFORM_ID else backend.provider.bind(TenantContext(tenant_id=owner))
            try:
                await box.destroy(env_id)
            except NotFoundError:
                pass
            report.orphans_destroyed += 1
        except ProviderError as err:
            report.errors.append(f"{env_id}：{err.code}")
            log.warning("删孤儿 %s 失败（%s）", env_id, err.code)
    return report


@dataclass(frozen=True)
class _Stub:
    """只写目录用的最小记录（目录里有、租户的行没有时）。"""

    env_ref: str
    owner: str
    backend: str


VOLUME_METRIC = "runtime.workspace.volume_used_ratio"


async def report_volume(broker: SandboxBroker, report: ReconcileReport, telemetry: TelemetryProvider | None) -> None:
    """挂卷的工作区所在卷的已用比例（DISK-A，WS-DISK-01 ③）：记进报告、经 Telemetry 报给看板与告警（超过 80% 告警）。
    没有挂卷的工作区根（只有存档模式的部署）时不报。"""
    root = broker.sandboxes.workspace_root
    if root is None or not Path(root).exists():
        return
    usage = await asyncio.to_thread(shutil.disk_usage, root)
    if usage.total <= 0:
        return
    report.volume_used_ratio = usage.used / usage.total
    if telemetry is not None:
        bound = telemetry.bind_platform("sandbox-reconcile")
        await bound.observe(VOLUME_METRIC, report.volume_used_ratio, attributes={"volume": "workspaces"})
        await bound.flush()


# 冷热分层的扫描（DISK-B）最多这么久一次：要逐个看工作区根下的目录与它们的记录，不必每轮都做。
COLD_SWEEP_EVERY = timedelta(hours=1)


class ReconcileActivities:
    def __init__(
        self, broker: SandboxBroker | None, *, hold_max: timedelta = DEFAULT_HOLD_MAX, telemetry: TelemetryProvider | None = None,
        cold_after_days: int = 0, runtime: str = "python", clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.broker = broker
        self.hold_max = hold_max
        self.telemetry = telemetry
        # 冷热分层的部署默认值（AR_WORKSPACE_COLD_AFTER_DAYS，0 不转冷；租户策略的 cold_after_days 优先）与转冷时借环境用的运行环境。
        self.cold_after_days = cold_after_days
        self.runtime = runtime
        self.clock = clock
        self._last_cold_sweep: datetime | None = None

    @activity.defn(name=names.RECONCILE_SANDBOXES)
    async def reconcile_sandboxes(self, inp: ReconcileInput) -> ReconcileReport:
        if self.broker is None:
            return ReconcileReport(skipped_backends=["这个 Worker 没有配置沙箱后端"])
        report = await reconcile_once(self.broker, grace=timedelta(seconds=inp.grace_s), hold_max=self.hold_max)
        try:
            await report_volume(self.broker, report, self.telemetry)
        except OSError as err:
            report.errors.append(f"工作区卷用量：{type(err).__name__}")
        now = self.clock()
        if self._last_cold_sweep is None or now - self._last_cold_sweep >= COLD_SWEEP_EVERY:
            self._last_cold_sweep = now
            try:
                report.cooled = await cool_idle_workspaces(self.broker, default_days=self.cold_after_days, now=now, runtime=self.runtime)
            except (ProviderError, OSError) as err:
                report.errors.append(f"冷热分层：{getattr(err, 'code', type(err).__name__)}")
        if report.orphans_destroyed or report.lost or report.reaped or report.errors or report.held or report.cooled:
            log.info(
                "沙箱对账：孤儿 %d、丢失 %d、回收 %d、重删 %d、等人保留 %d、转冷 %d、错误 %d",
                report.orphans_destroyed, report.lost, report.reaped, report.destroy_retried, report.held, report.cooled, len(report.errors),
            )
        return report

    def activities(self) -> dict[str, object]:
        return {names.RECONCILE_SANDBOXES: self.reconcile_sandboxes}


__all__ = ["VOLUME_METRIC", "ReconcileActivities", "reconcile_once", "report_volume"]
