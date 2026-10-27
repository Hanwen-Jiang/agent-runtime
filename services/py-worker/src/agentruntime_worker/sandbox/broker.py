"""SandboxBroker：Worker 里建、用、销毁沙箱环境都经它（D-11、D-12、D-13；docs/plan/sandbox-platform.md）。

- 选后端：租户策略的首选，暂时不可用（transient：容量满、故障）时按回退列表依次试；隔离强度按 none < container < microvm，
  回退的后端不能比 min_isolation（没写时取首选的）更弱；denied、invalid 原样报出，不回退（W11）。绑定了 workspace 时
  只在共用同一个工作区卷的后端之间回退（工作区跨后端要等 D-06 的存档与恢复）。落点与回退原因记进环境表与 span。
- 配额与预算：建环境前在环境表的事务里核对（W10，SandboxLedger.begin_create）。
- 会话级复用（W5）：reuse 时先按（租户，工作区，会话）在环境表里找没到期的 ready 环境，找不到才建；任何 Worker 副本都一样，
  不靠进程内的缓存。用完不删（release(keep=True)），空闲到期或活满时由对账回收（sandbox.reconcile）。
- 一个工作区同一时间只有一个可写的环境：别的会话占着、但那边没有命令在跑时，回收它再建；正在跑就报 denied（workspace_busy）。
- 没有环境表（ledger 为 None，只在单元测试与没配业务库的开发里）时退回原来的做法：直接调后端，不复用、不核对配额。
- 环境的短期凭据（D-15）：配了 credentials 时，每次借到的环境带上这条命令要的环境变量（Lease.env：包镜像的地址与凭据，
  sandbox.credentials），调用方经 exec 的 env 给到命令；不进 repr、环境表与 trace。
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from agentruntime_core.dev._common import PLATFORM_ID
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError, ProviderError, TransientError
from agentruntime_core.providers.sandbox import ExecResult, Sandbox, SandboxSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.credentials import EnvCredentials
from agentruntime_worker.sandbox.ledger import EnvRecord, SandboxLedger
from agentruntime_worker.sandbox.policy import ISOLATION_RANK, SandboxPolicy

if TYPE_CHECKING:
    # 运行时不导入：tools 包的 __init__ 又导入本模块。
    from agentruntime_worker.tools.sandboxes import Backend, Sandboxes

log = logging.getLogger(__name__)


@dataclass
class Lease:
    """一次借用：落在哪个后端、哪个环境；record 为 None 表示没有环境表。"""

    backend: Backend
    box: Sandbox
    env_id: str
    record: EnvRecord | None
    reused: bool = False
    fallback_from: str = ""
    fallback_reason: str = ""
    # 给这条命令的环境变量（包镜像与出网代理的地址、凭据）；里面有凭据，不进 repr。
    env: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def attributes(self) -> dict[str, Any]:
        """给 runtime.sandbox span 的属性（不含命令、输出与凭据）。"""
        out: dict[str, Any] = {"sandbox.backend": self.backend.name, "sandbox.kind": self.backend.kind, "sandbox.isolation": self.backend.isolation}
        if self.record is not None:
            out["sandbox.env_ref"] = self.record.env_ref
            out["sandbox.reused"] = self.reused
            if self.record.runtime:
                out["sandbox.runtime"] = self.record.runtime
            if self.record.image:
                out["sandbox.image"] = self.record.image
        if self.fallback_from:
            out["sandbox.fallback_from"] = self.fallback_from
            out["sandbox.fallback_reason"] = self.fallback_reason
        return out


class SandboxBroker:
    def __init__(self, sandboxes: Sandboxes, ledger: SandboxLedger | None = None, *, credentials: EnvCredentials | None = None) -> None:
        self.sandboxes = sandboxes
        self.ledger = ledger
        self.credentials = credentials

    # --- 借 ---

    async def acquire(
        self,
        tenant: TenantContext,
        spec: SandboxSpec,
        *,
        task_id: str = "",
        writable: bool = True,
        reuse: bool = False,
        busy_for: timedelta | None = None,
    ) -> Lease:
        owner = tenant.tenant_id or PLATFORM_ID
        policy = await self.sandboxes.policy_for(owner)
        session = tenant.session_id
        if reuse and session and self.ledger is not None:
            # 没绑工作区的环境不算可写（与 begin_create 的记法一致）。
            found = await self.ledger.find_reusable(
                owner, workspace_id=spec.workspace_id, session_id=session, backends=policy.names, writable=writable and bool(spec.workspace_id)
            )
            if found is not None:
                backend = await self.sandboxes.backend(found.backend)
                record = await self.ledger.touch(found, task_id=task_id, busy_for=busy_for)
                lease = Lease(backend, backend.provider.bind(tenant), record.backend_env_id, record, reused=True)
                return await self._with_env(lease, policy)
        lease = await self._create(tenant, owner, spec, policy, task_id=task_id, writable=writable, reuse=reuse, busy_for=busy_for)
        return await self._with_env(lease, policy)

    async def _with_env(self, lease: Lease, policy: SandboxPolicy) -> Lease:
        if self.credentials is not None and lease.record is not None:
            lease.env = await self.credentials.exec_env(lease.record, self.sandboxes.spec(lease.backend.name), policy.quota)
        return lease

    async def _create(
        self, tenant: TenantContext, owner: str, spec: SandboxSpec, policy: SandboxPolicy, *, task_id: str, writable: bool, reuse: bool,
        busy_for: timedelta | None,
    ) -> Lease:
        candidates, skipped = self._candidates(policy, spec)
        if not candidates:
            raise InvalidError("no_sandbox_backend", "租户策略里没有满足隔离强度与工作区要求的沙箱后端：" + "；".join(skipped))
        preferred = policy.backend
        errors: list[str] = []
        idle = min(spec.idle_timeout, policy.idle_timeout) if not reuse else policy.idle_timeout
        for name in candidates:
            backend = await self.sandboxes.backend(name)
            fallback_from = preferred if name != preferred else ""
            reason = errors[-1] if errors else ""
            record = None
            if self.ledger is not None:
                record = await self._begin(owner, backend, spec, policy, task_id=task_id, writable=writable, reuse=reuse, idle=idle,
                                           fallback_from=fallback_from, tenant=tenant)
            box = backend.provider.bind(tenant)
            try:
                env = await box.create(replace(spec, idle_timeout=max(spec.idle_timeout, idle)))
            except TransientError as err:
                errors.append(f"{name}：{err.code}")
                if record is not None:
                    await self.ledger.mark_failed(record, f"{err.code}: {err.message}")  # type: ignore[union-attr]
                log.warning("沙箱后端 %s 暂时不可用（%s），试下一个", name, err.code)
                continue
            except ProviderError as err:
                if record is not None:
                    await self.ledger.mark_failed(record, f"{err.code}: {err.message}")  # type: ignore[union-attr]
                raise
            if record is not None:
                record = await self.ledger.mark_ready(record, env.env_id, image=env.image)  # type: ignore[union-attr]
                if busy_for is not None:
                    record = await self.ledger.touch(record, task_id=task_id, busy_for=busy_for)  # type: ignore[union-attr]
            return Lease(backend, box, env.env_id, record, fallback_from=fallback_from, fallback_reason=reason)
        tried = "、".join(errors)
        weaker = [s for s in skipped if "隔离强度" in s]
        hint = f"；回退列表里更弱的后端不用：{'、'.join(weaker)}" if weaker else ""
        raise TransientError("sandbox_unavailable", f"沙箱后端都暂时不可用（{tried}）{hint}")

    async def _begin(
        self, owner: str, backend: Backend, spec: SandboxSpec, policy: SandboxPolicy, *, task_id: str, writable: bool, reuse: bool,
        idle: timedelta, fallback_from: str, tenant: TenantContext,
    ) -> EnvRecord:
        assert self.ledger is not None
        kwargs: dict[str, Any] = dict(
            backend=backend.name, kind=backend.kind, isolation=backend.isolation, workspace_id=spec.workspace_id,
            session_id=tenant.session_id if reuse else "", task_id=task_id, writable=writable and bool(spec.workspace_id),
            runtime=spec.runtime, image=self.sandboxes.image_for(backend.name, spec.runtime), idle=idle,
            max_lifetime=policy.max_lifetime, quota=policy.quota, fallback_from=fallback_from,
        )
        try:
            return await self.ledger.begin_create(owner, **kwargs)
        except DeniedError as err:
            if err.code != "workspace_busy":
                raise
        # 别的会话占着这个工作区的可写环境：那边闲着就回收，再建一次；正在跑命令就报给调用方。
        holders = await self.ledger.writable_holders(owner, workspace_id=spec.workspace_id, session_id=kwargs["session_id"])
        if any(not self.ledger.idle_now(h) for h in holders):
            raise DeniedError("workspace_busy", "这个工作区正被另一个会话的可写环境用着（命令还在跑），稍后再试")
        for holder in holders:
            await self._destroy_record(holder, "preempted: 另一个会话要写同一个工作区")
        return await self.ledger.begin_create(owner, **kwargs)

    def _candidates(self, policy: SandboxPolicy, spec: SandboxSpec) -> tuple[list[str], list[str]]:
        names = policy.names
        floor = policy.min_isolation or self.sandboxes.isolation(names[0])
        floor_rank = ISOLATION_RANK[floor]
        group = self._workspace_group(names[0]) if spec.workspace_id else None
        out: list[str] = []
        skipped: list[str] = []
        for name in names:
            if ISOLATION_RANK[self.sandboxes.isolation(name)] < floor_rank:
                skipped.append(f"{name}（隔离强度 {self.sandboxes.isolation(name)} 低于 {floor}）")
                continue
            if spec.workspace_id and self._workspace_group(name) != group:
                skipped.append(f"{name}（与首选不共用工作区）")
                continue
            out.append(name)
        return out, skipped

    def _workspace_group(self, name: str) -> str | None:
        """能挂同一个工作区的后端算一组：同一个 PVC 的 K8s 后端、本地实现；托管后端各自一组（还挂不了工作区）。"""
        spec = self.sandboxes.spec(name)
        if spec.get("kind") == "k8s":
            return "pvc:" + str(spec.get("workspace_claim") or "")
        if spec.get("kind") == "local":
            return "local"
        return "none:" + name

    # --- 还 ---

    async def release(self, lease: Lease, *, result: ExecResult | None = None, keep: bool = False) -> None:
        """用完：会话级的环境留着（顺延空闲、记 CPU 时间），一次性的销毁。"""
        cpu_ms = int(result.cpu_time.total_seconds() * 1000) if result is not None and result.cpu_time is not None else 0
        if lease.record is None:
            if not keep:
                with contextlib.suppress(ProviderError):
                    await lease.box.destroy(lease.env_id)
            return
        assert self.ledger is not None
        if keep:
            lease.record = await self.ledger.touch(lease.record, cpu_ms=cpu_ms)
            return
        lease.record = await self.ledger.touch(lease.record, cpu_ms=cpu_ms)
        await self._destroy(lease)

    async def discard(self, lease: Lease, reason: str) -> None:
        """后端里已经没有这个环境（被回收、节点重启）：记为丢失，尽量删掉残留。"""
        if lease.record is not None and self.ledger is not None:
            lease.record = await self.ledger.mark_lost(lease.record, reason)
        with contextlib.suppress(ProviderError):
            await lease.box.destroy(lease.env_id)

    async def _destroy(self, lease: Lease) -> None:
        assert self.ledger is not None and lease.record is not None
        record = await self.ledger.begin_destroy(lease.record)
        try:
            await lease.box.destroy(lease.env_id)
        except NotFoundError:
            pass
        except ProviderError as err:
            # 留在 destroying，由对账再删。
            log.warning("销毁沙箱环境 %s 失败（%s），交给对账", record.env_ref, err.code)
            lease.record = record
            return
        lease.record = await self.ledger.mark_destroyed(record, **self._prices(record.backend))

    async def _destroy_record(self, record: EnvRecord, reason: str) -> EnvRecord:
        """按环境表的一行销毁（对账、回收、抢占用）；后端里已经没了也算销毁完成。"""
        assert self.ledger is not None
        backend = await self.sandboxes.backend(record.backend)
        box = backend.provider.bind_platform("sandbox-reconcile") if record.owner == PLATFORM_ID else backend.provider.bind(
            TenantContext(tenant_id=record.owner, workspace_id=record.workspace_id)
        )
        record = await self.ledger.begin_destroy(record, reason)
        if record.backend_env_id:
            try:
                await box.destroy(record.backend_env_id)
            except NotFoundError:
                pass
        return await self.ledger.mark_destroyed(record, **self._prices(record.backend))

    def _prices(self, backend: str) -> dict[str, float]:
        price = dict(self.sandboxes.spec(backend).get("price") or {})
        return {"price_per_hour": float(price.get("per_hour") or 0), "price_per_cpu_hour": float(price.get("per_cpu_hour") or 0)}

    # --- 批量 ---

    async def destroy_session(self, tenant_id: str, *, workspace_id: str, session_id: str) -> int:
        """Session 关闭时销毁它的环境（W5 第 1 条）；返回销毁了几个。"""
        if self.ledger is None:
            return 0
        n = 0
        for record in await self.ledger.active(tenant_id):
            if record.session_id == session_id and record.workspace_id == workspace_id and record.state == "ready":
                await self._destroy_record(record, "session closed")
                n += 1
        return n

    async def destroy_tenant(self, tenant_id: str) -> int:
        """租户删除时销毁它的全部环境（W9；设计书还没有租户删除的流程，这里先给出沙箱一侧要做的）。"""
        if self.ledger is None:
            return 0
        n = 0
        for record in await self.ledger.active(tenant_id):
            if record.state == "creating":
                await self.ledger.mark_failed(record, "tenant deleted")
                continue
            await self._destroy_record(record, "tenant deleted")
            n += 1
        return n


__all__ = ["Lease", "SandboxBroker"]
