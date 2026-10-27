"""沙箱环境表的对账（W9；D-11；docs/plan/sandbox-platform.md 第 1 节）。

工作流在 workflows/sandbox_reconcile.py（平台级、固定的工作流 ID，每隔 interval_s 跑一次 reconcile_sandboxes activity）；
这里是 activity 与它做的事。

一轮做的事（reconcile_once）：
1. 列出每个能列的后端在跑的环境（列不了的后端这一轮跳过，不据此判丢失）。
2. 平台目录里没结束的环境，逐个绑定到所属租户读它的行：
   - creating 超过宽限还没就绪：记为 failed（后端要是已经建了，下面当孤儿删掉）；
   - ready 而后端的列表里没有：记为丢失（不在后端重建）；
   - ready 且空闲到期或活满、没有命令在跑：销毁（会话级沙箱的回收，W5）；登记了 pause_on_idle 的后端（E2B）空闲到期时
     后端自己暂停，这里只在活满时销毁；
   - destroying：再删一次，删掉了（或后端里已经没了）记为 destroyed。
   目录里有、租户的行没有（写了目录、没写成行就中断了）：超过宽限把目录记为 failed。
3. 后端列出来的环境，环境表里没有或已经结束的：超过宽限（按后端记的创建时间）就是孤儿，销毁。只碰本部署建的环境
   （K8s 按标签、桥按令牌），作用域从后端记的 owner 来，不猜。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from temporalio import activity

from agentruntime_core.dev._common import PLATFORM_ID
from agentruntime_core.errors import NotFoundError, ProviderError
from agentruntime_core.providers.sandbox import ListedEnvironment
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker import names
from agentruntime_worker.sandbox.ledger import iso, parse_iso
from agentruntime_worker.workflows.sandbox_reconcile import ReconcileInput, ReconcileReport

if TYPE_CHECKING:
    from agentruntime_worker.sandbox.broker import SandboxBroker

log = logging.getLogger(__name__)
_OWNER = re.compile(r"^(_platform|[a-z0-9][a-z0-9_-]{0,62})$")


async def reconcile_once(broker: SandboxBroker, *, grace: timedelta = timedelta(minutes=10)) -> ReconcileReport:
    report = ReconcileReport()
    ledger = broker.ledger
    if ledger is None:
        return report
    sandboxes = broker.sandboxes
    now = ledger.clock()
    listings: dict[str, dict[str, ListedEnvironment]] = {}
    for name in sandboxes.names:
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
                elif _reapable(broker, record, now) and ledger.idle_now(record):
                    await broker._destroy_record(record, "idle expired" if record.expires_at <= iso(now) else "max lifetime")  # noqa: SLF001
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


def _reapable(broker: SandboxBroker, record: object, now: datetime) -> bool:
    """该回收了：空闲到期或活满。登记里写了 pause_on_idle 的后端（e2b 分支的 E2B：空闲到期时自己暂停，内存与进程都留着）
    空闲到期不删，只在活满时删。"""
    expires_at, max_until = getattr(record, "expires_at", ""), getattr(record, "max_until", "")
    lifetime = bool(max_until and max_until <= iso(now))
    try:
        pauses = broker.sandboxes.spec(getattr(record, "backend", "")).get("pause_on_idle") is True
    except ProviderError:
        pauses = False
    return lifetime if pauses else (lifetime or bool(expires_at and expires_at <= iso(now)))


@dataclass(frozen=True)
class _Stub:
    """只写目录用的最小记录（目录里有、租户的行没有时）。"""

    env_ref: str
    owner: str
    backend: str


class ReconcileActivities:
    def __init__(self, broker: SandboxBroker | None) -> None:
        self.broker = broker

    @activity.defn(name=names.RECONCILE_SANDBOXES)
    async def reconcile_sandboxes(self, inp: ReconcileInput) -> ReconcileReport:
        if self.broker is None:
            return ReconcileReport(skipped_backends=["这个 Worker 没有配置沙箱后端"])
        report = await reconcile_once(self.broker, grace=timedelta(seconds=inp.grace_s))
        if report.orphans_destroyed or report.lost or report.reaped or report.errors:
            log.info(
                "沙箱对账：孤儿 %d、丢失 %d、回收 %d、重删 %d、错误 %d",
                report.orphans_destroyed, report.lost, report.reaped, report.destroy_retried, len(report.errors),
            )
        return report

    def activities(self) -> dict[str, object]:
        return {names.RECONCILE_SANDBOXES: self.reconcile_sandboxes}


__all__ = ["ReconcileActivities", "reconcile_once"]
