"""SandboxBroker：Worker 里建、用、销毁沙箱环境都经它（D-11、D-12、D-13；docs/plan/sandbox-platform.md）。

- 选后端：租户策略的首选，暂时不可用（transient：容量满、故障）时按回退列表依次试；隔离强度按 none < container < microvm，
  回退的后端不能比 min_isolation（没写时取首选的）更弱；denied、invalid 原样报出，不回退（W11）。绑定了 workspace 时
  只在同一组后端之间回退：挂同一个工作区卷的算一组，存档模式的（workspace: archive）都算一组。落点与回退原因记进环境表与 span。
- 存档模式（D-06，docs/plan/workspace-layer.md 第 4 节）：后端不挂工作区卷，建环境后从最近的快照恢复（不一致时换一个环境再试
  一次，仍不一致报 invalid），可写的环境销毁之前先存档（还、回收、被抢占、换环境都一样）；存档失败时暂时性的留着环境、由下一轮
  对账再试，活满或永久性的失败照样销毁。每个环境各有一份工作区，所以一个工作区同一时间只有一个可写的环境，不分会话。
- 配额与预算：建环境前在环境表的事务里核对（W10，SandboxLedger.begin_create）。
- 会话级复用（W5）：reuse 时先按（租户，工作区，会话）在环境表里找没到期的 ready 环境，找不到才建；任何 Worker 副本都一样，
  不靠进程内的缓存。用完不删（release(keep=True)），空闲到期或活满时由对账回收（sandbox.reconcile）。
- 一个工作区同一时间只有一个可写的环境：别的会话占着、但那边没有命令在跑时，回收它再建；正在跑就报 denied（workspace_busy）。
- 没有环境表（ledger 为 None，只在单元测试与没配业务库的开发里）时退回原来的做法：直接调后端，不复用、不核对配额。
- 环境的短期凭据（D-15、D-16）：配了 credentials 时，每次借到的环境带上这条命令要的环境变量（Lease.env：包镜像与出网网关
  的地址与凭据、租户的测试用密钥，sandbox.credentials），调用方经 exec 的 env 给到命令；不进 repr、环境表与 trace。
- 联网授权（D-16，sandbox.grants）：授权属于 Task。经出网网关的后端（K8s、本地）环境本身断网，网关按授权放行；托管后端
  没有网关，Task 有授权时环境建成按域名放行（network = allowlist，主机是各授权的并集），授权变了就换一个环境。授权带用量
  上限（字节、连接、频率）时只能落在经网关的后端（W11：要审计的 Task 只在支持网关的后端之间回退）。
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from agentruntime_core.dev._common import PLATFORM_ID
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError, ProviderError, TransientError
from agentruntime_core.providers.sandbox import ExecResult, Sandbox, SandboxSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.credentials import EnvCredentials
from agentruntime_worker.sandbox.environments import EnvironmentManager, same_image
from agentruntime_worker.sandbox.grants import Grant, GrantStore, union_hosts
from agentruntime_worker.sandbox.ledger import EnvRecord, SandboxLedger
from agentruntime_worker.sandbox.policy import ISOLATION_RANK, SandboxPolicy

if TYPE_CHECKING:
    # 运行时不导入：tools 包的 __init__ 又导入本模块。
    from agentruntime_worker.tools.sandboxes import Backend, Sandboxes
    from agentruntime_worker.workspace.archive import WorkspaceArchiver

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
    # 给这条命令的环境变量（包镜像与出网代理的地址、凭据，测试用密钥）；里面有凭据，不进 repr。
    env: dict[str, str] = field(default_factory=dict, repr=False)
    # 联网的方式：none（断网）、gateway（经出网网关，按授权放行）、allowlist（托管后端按域名放行）；注入的测试用密钥的名字。
    network: str = "none"
    test_env_names: tuple[str, ...] = ()
    # 工作区（D-06）：所属作用域、工作区、是否存档模式（建时恢复、还时存档）、是否可写；restored 是恢复用的快照。
    owner: str = ""
    workspace_id: str = ""
    archive: bool = False
    writable: bool = False
    restored: str = ""

    @property
    def attributes(self) -> dict[str, Any]:
        """给 runtime.sandbox span 的属性（不含命令、输出与凭据；测试用密钥只记名字）。"""
        out: dict[str, Any] = {"sandbox.backend": self.backend.name, "sandbox.kind": self.backend.kind, "sandbox.isolation": self.backend.isolation}
        out["sandbox.network"] = self.network
        if self.test_env_names:
            out["sandbox.test_env"] = list(self.test_env_names)
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
        if self.archive:
            out["workspace.mode"] = "archive"
            if self.restored:
                out["workspace.restored_snapshot"] = self.restored
        return out


class SandboxBroker:
    def __init__(
        self, sandboxes: Sandboxes, ledger: SandboxLedger | None = None, *, credentials: EnvCredentials | None = None,
        archiver: WorkspaceArchiver | None = None,
    ) -> None:
        self.sandboxes = sandboxes
        self.ledger = ledger
        self.credentials = credentials
        self.grants = GrantStore(ledger) if ledger is not None else None
        # 按项目定制的运行环境（D-17）：shell_run 借环境之前问它用哪个镜像；没配时为 None。
        self.environments: EnvironmentManager | None = None
        # 存档模式的工作区（D-06）：没配时存档模式的后端不能绑定工作区（invalid）。
        self.archiver = archiver
        # 会话级环境在它的 Task 等人时最多保留多久（docs/plan/feedback-2026-11-01.md N-05）：对账据此顺延。后端自己的空闲到期
        # （K8s 的注解、Cloudflare 的休眠）要多给这么多，否则后端先把环境删了；什么时候回收仍由对账按环境表决定。
        self.hold_max = timedelta(0)

    def archive_mode(self, name: str) -> bool:
        return self.sandboxes.workspace_mode(name) == "archive"

    async def archive_mode_for(self, tenant_id: str) -> bool:
        """这个租户的工作区是不是存档模式（按首选后端；回退只在同一组里，所以组里都一样）。"""
        return self.archive_mode((await self.sandboxes.policy_for(tenant_id)).backend)

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
        grants = await self.task_grants(owner, task_id)
        session = tenant.session_id
        if reuse and session and self.ledger is not None:
            found = None
            # 没绑工作区的环境不算可写（与 begin_create 的记法一致）。存档模式下读的也先找本会话可写的环境：
            # 只读的环境是在快照上建的，看不到本会话刚写的内容。
            prefer_writable = not writable and bool(spec.workspace_id) and self.archive_mode(policy.backend)
            for want in ((True, False) if prefer_writable else (writable and bool(spec.workspace_id),)):
                found = await self.ledger.find_reusable(owner, workspace_id=spec.workspace_id, session_id=session, backends=policy.names, writable=want)
                if found is not None:
                    break
            if found is not None and (not self._fits_grants(found.backend, found, grants) or (spec.image and not same_image(spec.image, found.image))):
                # 托管后端的域名白名单是建环境时定的：授权变了（或要审计却不经网关），这个环境不能再用，闲着就换掉；
                # 项目环境的镜像构建好了（D-17），之前用 universal 建的环境也换掉。
                if self.ledger.idle_now(found):
                    await self._destroy_record(found, "network grants or environment image changed")
                found = None
            if found is not None:
                backend = await self.sandboxes.backend(found.backend)
                box = backend.provider.bind(tenant)
                healthy = False
                probe = getattr(box, "healthy", None)
                if probe is not None:
                    # Same environment at a real lease boundary, never a global scan or a query-side probe.
                    with contextlib.suppress(ProviderError):
                        healthy = await probe(found.backend_env_id)
                record = await self.ledger.touch(found, task_id=task_id, busy_for=busy_for, health_confirmed=healthy, reused=True)
                lease = Lease(
                    backend, box, record.backend_env_id, record, reused=True, owner=owner, workspace_id=found.workspace_id,
                    archive=bool(found.workspace_id) and self.archive_mode(found.backend), writable=bool(found.writable),
                )
                return await self._with_env(lease, policy, grants)
        lease = await self._create(tenant, owner, spec, policy, task_id=task_id, writable=writable, reuse=reuse, busy_for=busy_for, grants=grants)
        return await self._with_env(lease, policy, grants)

    async def task_grants(self, owner: str, task_id: str) -> list[Grant]:
        """这个 Task 现在有效的联网授权（没有环境表或没有 Task 时为空）。"""
        if self.grants is None or not task_id or owner == PLATFORM_ID:
            return []
        return await self.grants.for_task(owner, task_id)

    def has_gateway(self, name: str) -> bool:
        return self.credentials is not None and bool(self.credentials.egress_for(self.sandboxes.spec(name)))

    def _allow_hosts(self, name: str, grants: list[Grant]) -> str:
        """托管后端按域名放行时环境要带的主机（JSON）；经网关的后端与没有授权时为空。"""
        if not grants or self.has_gateway(name):
            return ""
        return json.dumps(list(union_hosts(grants)))

    def _fits_grants(self, name: str, record: EnvRecord, grants: list[Grant]) -> bool:
        if any(g.audited for g in grants) and not self.has_gateway(name):
            return False
        return record.allow_hosts == self._allow_hosts(name, grants)

    async def _with_env(self, lease: Lease, policy: SandboxPolicy, grants: list[Grant] | None = None) -> Lease:
        lease.network = "none"
        if self.has_gateway(lease.backend.name):
            lease.network = "gateway"
        elif lease.record is not None and lease.record.allow_hosts:
            lease.network = "allowlist"
        if self.credentials is not None and lease.record is not None:
            lease.env = await self.credentials.exec_env(lease.record, self.sandboxes.spec(lease.backend.name), policy.quota)
            secrets = await self.credentials.test_env(lease.record.owner)
            lease.env.update(secrets)
            lease.test_env_names = tuple(sorted(secrets))
        return lease

    async def _create(
        self, tenant: TenantContext, owner: str, spec: SandboxSpec, policy: SandboxPolicy, *, task_id: str, writable: bool, reuse: bool,
        busy_for: timedelta | None, grants: list[Grant] | None = None,
    ) -> Lease:
        grants = grants or []
        candidates, skipped = self._candidates(policy, spec, gateway_only=any(g.audited for g in grants))
        if not candidates:
            raise InvalidError("no_sandbox_backend", "租户策略里没有满足隔离强度、工作区与出网审计要求的沙箱后端：" + "；".join(skipped))
        preferred = policy.backend
        errors: list[str] = []
        idle = min(spec.idle_timeout, policy.idle_timeout) if not reuse else policy.idle_timeout
        for name in candidates:
            backend = await self.sandboxes.backend(name)
            fallback_from = preferred if name != preferred else ""
            reason = errors[-1] if errors else ""
            allow_hosts = self._allow_hosts(name, grants)
            archive = bool(spec.workspace_id) and self.archive_mode(name)
            if archive:
                if self.archiver is None:
                    raise InvalidError("workspace_archive_unavailable", f"沙箱后端 {name} 是存档模式，这个 Worker 没有配工作区的存档")
                # 第一次用到这个工作区时补一行，按租户策略的工作区个数核对（D-06）。
                await self.archiver.ensure(owner, spec.workspace_id, policy.workspace)
            # 存档模式下恢复的结果与清单不一致时换一个环境再试一次（workspace-layer.md 第 2 节）。
            for attempt in range(2 if archive else 1):
                lease = await self._create_one(
                    tenant, owner, backend, spec, policy, task_id=task_id, writable=writable, reuse=reuse, busy_for=busy_for, idle=idle,
                    fallback_from=fallback_from, allow_hosts=allow_hosts, archive=archive, errors=errors,
                )
                if lease is None:
                    break  # 这个后端暂时不可用：试下一个
                lease.fallback_reason = reason
                if not archive:
                    return lease
                try:
                    snap = await self.archiver.restore(  # type: ignore[union-attr]
                        lease.box, lease.env_id, owner, spec.workspace_id, max_bytes=policy.workspace.max_bytes
                    )
                except TransientError as err:
                    await self._destroy(lease, snapshot=False, reason=f"restore failed: {err.code}")
                    if err.code != "workspace_restore_mismatch":
                        raise
                    if attempt == 1:
                        raise InvalidError("workspace_restore_mismatch", "工作区的快照恢复了两次都与清单不一致，不在半个工作区上执行") from None
                    log.warning("工作区 %s 恢复后与清单不一致，换一个环境再试", spec.workspace_id)
                    continue
                except BaseException:
                    with contextlib.suppress(ProviderError):
                        await self._destroy(lease, snapshot=False, reason="restore failed")
                    raise
                lease.restored = snap.snapshot_id if snap is not None else ""
                return lease
        tried = "、".join(errors)
        weaker = [s for s in skipped if "隔离强度" in s]
        hint = f"；回退列表里更弱的后端不用：{'、'.join(weaker)}" if weaker else ""
        raise TransientError("sandbox_unavailable", f"沙箱后端都暂时不可用（{tried}）{hint}")

    async def _create_one(
        self, tenant: TenantContext, owner: str, backend: Backend, spec: SandboxSpec, policy: SandboxPolicy, *, task_id: str, writable: bool,
        reuse: bool, busy_for: timedelta | None, idle: timedelta, fallback_from: str, allow_hosts: str, archive: bool, errors: list[str],
    ) -> Lease | None:
        """在一个后端上建一个环境；后端暂时不可用（transient）时记进 errors、返回 None，别的错误照报。"""
        name = backend.name
        record = None
        if self.ledger is not None:
            record = await self._begin(owner, backend, spec, policy, task_id=task_id, writable=writable, reuse=reuse, idle=idle,
                                       fallback_from=fallback_from, tenant=tenant, allow_hosts=allow_hosts, exclusive=archive)
        box = backend.provider.bind(tenant)
        create_spec = replace(spec, idle_timeout=max(spec.idle_timeout, idle) + (self.hold_max if reuse and self.ledger is not None else timedelta(0)))
        if archive:
            create_spec = replace(create_spec, workspace_id="")  # 不挂卷：建好后从快照恢复
        if allow_hosts:
            create_spec = replace(create_spec, network="allowlist", allow_hosts=tuple(json.loads(allow_hosts)))
        try:
            env = await box.create(create_spec)
        except TransientError as err:
            errors.append(f"{name}：{err.code}")
            if record is not None:
                await self.ledger.mark_failed(record, f"{err.code}: {err.message}")  # type: ignore[union-attr]
            log.warning("沙箱后端 %s 暂时不可用（%s），试下一个", name, err.code)
            return None
        except ProviderError as err:
            if record is not None:
                await self.ledger.mark_failed(record, f"{err.code}: {err.message}")  # type: ignore[union-attr]
            raise
        if record is not None:
            record = await self.ledger.mark_ready(record, env.env_id, image=env.image, health_confirmed=env.health_confirmed)  # type: ignore[union-attr]
            if busy_for is not None:
                record = await self.ledger.touch(record, task_id=task_id, busy_for=busy_for)  # type: ignore[union-attr]
        return Lease(
            backend, box, env.env_id, record, fallback_from=fallback_from, owner=owner, workspace_id=spec.workspace_id, archive=archive,
            writable=writable and bool(spec.workspace_id),
        )

    async def _begin(
        self, owner: str, backend: Backend, spec: SandboxSpec, policy: SandboxPolicy, *, task_id: str, writable: bool, reuse: bool,
        idle: timedelta, fallback_from: str, tenant: TenantContext, allow_hosts: str = "", exclusive: bool = False,
    ) -> EnvRecord:
        assert self.ledger is not None
        kwargs: dict[str, Any] = dict(
            backend=backend.name, kind=backend.kind, isolation=backend.isolation, workspace_id=spec.workspace_id,
            session_id=tenant.session_id if reuse else "", task_id=task_id, writable=writable and bool(spec.workspace_id),
            runtime=spec.runtime, image=spec.image or self.sandboxes.image_for(backend.name, spec.runtime), idle=idle,
            max_lifetime=policy.max_lifetime, quota=policy.quota, fallback_from=fallback_from, allow_hosts=allow_hosts,
            exclusive=exclusive and writable,
        )
        try:
            return await self.ledger.begin_create(owner, **kwargs)
        except DeniedError as err:
            if err.code != "workspace_busy":
                raise
        # 别的会话（存档模式下不分会话）占着这个工作区的可写环境：那边闲着就回收（存档模式先存档），再建一次；
        # 正在跑命令就报给调用方。
        holders = await self.ledger.writable_holders(
            owner, workspace_id=spec.workspace_id, session_id=None if kwargs["exclusive"] else kwargs["session_id"]
        )
        if any(not self.ledger.idle_now(h) for h in holders):
            raise DeniedError("workspace_busy", "这个工作区正被另一个可写的环境用着（命令还在跑），稍后再试")
        why = "preempted: 另一个环境要写同一个工作区（存档模式）" if kwargs["exclusive"] else "preempted: 另一个会话要写同一个工作区"
        for holder in holders:
            await self._destroy_record(holder, why)
        return await self.ledger.begin_create(owner, **kwargs)

    def _candidates(self, policy: SandboxPolicy, spec: SandboxSpec, *, gateway_only: bool = False) -> tuple[list[str], list[str]]:
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
            if gateway_only and not self.has_gateway(name):
                skipped.append(f"{name}（不经出网网关，做不到联网授权的用量上限与审计）")
                continue
            out.append(name)
        return out, skipped

    def _workspace_group(self, name: str) -> str | None:
        """能用同一个工作区的后端算一组：存档模式的都是一组（内容在 ObjectStore 里，D-06）；挂卷的按卷分：同一个 PVC 的
        K8s 后端、本地实现；没登记存档模式的托管后端各自一组（挂不了工作区）。"""
        spec = self.sandboxes.spec(name)
        if self.archive_mode(name):
            return "archive"
        if spec.get("kind") == "k8s":
            return "pvc:" + str(spec.get("workspace_claim") or "")
        if spec.get("kind") == "local":
            return "local"
        return "none:" + name

    # --- 还 ---

    async def release(self, lease: Lease, *, result: ExecResult | None = None, keep: bool = False) -> None:
        """用完：会话级的环境留着（顺延空闲、记 CPU 时间），一次性的销毁（存档模式的可写环境先存档）。存档暂时失败时环境
        留着、报错，由下一轮对账或下一个要写的环境再存；永久性的失败（invalid，例如超过大小上限）照样销毁、报错。"""
        cpu_ms = int(result.cpu_time.total_seconds() * 1000) if result is not None and result.cpu_time is not None else 0
        if lease.record is not None:
            assert self.ledger is not None
            lease.record = await self.ledger.touch(lease.record, cpu_ms=cpu_ms)
        if keep:
            return
        if lease.archive and lease.writable:
            try:
                await self.snapshot(lease)
            except InvalidError as err:
                await self._destroy(lease, reason=f"存档失败：{err.code}")
                raise
        if lease.record is None:
            with contextlib.suppress(ProviderError):
                await lease.box.destroy(lease.env_id)
            return
        await self._destroy(lease)

    async def snapshot(self, lease: Lease) -> Any:
        """把借到的环境里的工作区存一份快照（存档模式；Workspace API 的立即存档也用它，挂卷的后端一样能导出）。"""
        if self.archiver is None or not lease.workspace_id:
            raise InvalidError("workspace_archive_unavailable", "这个 Worker 没有配工作区的存档，或环境没有绑定工作区")
        limits = (await self.sandboxes.policy_for(lease.owner)).workspace
        env_ref = lease.record.env_ref if lease.record is not None else ""
        return await self.archiver.snapshot(lease.box, lease.env_id, lease.owner, lease.workspace_id, max_bytes=limits.max_bytes, env_ref=env_ref)

    async def discard(self, lease: Lease, reason: str) -> None:
        """后端里已经没有这个环境（被回收、节点重启）：记为丢失，尽量删掉残留。"""
        if lease.record is not None and self.ledger is not None:
            lease.record = await self.ledger.mark_lost(lease.record, reason)
        with contextlib.suppress(ProviderError):
            await lease.box.destroy(lease.env_id)

    async def _destroy(self, lease: Lease, *, snapshot: bool = False, reason: str = "") -> None:
        if snapshot and lease.archive and lease.writable:
            await self.snapshot(lease)
        if lease.record is None or self.ledger is None:
            with contextlib.suppress(ProviderError):
                await lease.box.destroy(lease.env_id)
            return
        record = await self.ledger.begin_destroy(lease.record, reason)
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

    def _archived(self, record: EnvRecord) -> bool:
        """这一行是存档模式下可写、还活着的环境：销毁之前要存档。"""
        return (
            self.archiver is not None and bool(record.workspace_id) and bool(record.writable) and record.state == "ready"
            and bool(record.backend_env_id) and record.backend in self.sandboxes.names and self.archive_mode(record.backend)
        )

    async def _destroy_record(self, record: EnvRecord, reason: str, *, snapshot: bool = True, force: bool = False) -> EnvRecord:
        """按环境表的一行销毁（对账、回收、抢占用）；后端里已经没了也算销毁完成。存档模式的可写环境先存档：存档暂时失败时
        报错、环境留着（force 时照样销毁，例如活满）；永久性的失败（invalid，例如超过大小上限）照样销毁，原因记进环境表。"""
        assert self.ledger is not None
        if snapshot and self._archived(record):
            backend = await self.sandboxes.backend(record.backend)
            box = backend.provider.bind(TenantContext(tenant_id=record.owner, workspace_id=record.workspace_id))
            lease = Lease(backend, box, record.backend_env_id, record, owner=record.owner, workspace_id=record.workspace_id, archive=True, writable=True)
            try:
                await self.snapshot(lease)
            except NotFoundError:
                return await self.ledger.mark_lost(record, "存档时后端里已经没有这个环境")
            except ProviderError as err:
                if not force and not isinstance(err, InvalidError):
                    raise
                log.warning("工作区 %s 销毁前存档失败（%s），照样销毁，这个环境里的改动丢了", record.workspace_id, err.code)
                reason = f"{reason}; 存档失败：{err.code}"
        if record.backend == BUILD_BACKEND:
            # 构建记的环境（BuildNetwork）：Job 由构建器自己删，这里只收尾记录与授权。
            if self.grants is not None and record.task_id:
                await self.grants.revoke_task(record.owner, record.task_id)
            record = await self.ledger.begin_destroy(record, reason)
            return await self.ledger.mark_destroyed(record)
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
        if self.grants is not None:
            await self.grants.revoke_all(tenant_id, "tenant deleted")
        n = 0
        for record in await self.ledger.active(tenant_id):
            if record.state == "creating":
                await self.ledger.mark_failed(record, "tenant deleted")
                continue
            await self._destroy_record(record, "tenant deleted", snapshot=False)
            n += 1
        if self.archiver is not None:
            # 工作区与它的存档一并删除（4.10.1 的“删除”）。
            for workspace in await self.archiver.list_workspaces(tenant_id):
                await self.archiver.delete(tenant_id, workspace.workspace_id)
        return n

    async def destroy_workspace(self, tenant_id: str, workspace_id: str) -> int:
        """删除工作区时销毁它的全部环境（不存档）；返回销毁了几个。正在跑命令的也销毁。"""
        if self.ledger is None:
            return 0
        n = 0
        for record in await self.ledger.active(tenant_id):
            if record.workspace_id != workspace_id:
                continue
            if record.state == "creating":
                await self.ledger.mark_failed(record, "workspace deleted")
                continue
            await self._destroy_record(record, "workspace deleted", snapshot=False)
            n += 1
        return n


# 构建项目环境时在环境表里记的“后端”（不在后端登记里；D-17）。
BUILD_BACKEND = "env-builder"


class BuildNetwork:
    """构建项目环境时的联网（D-17）：在环境表里记一行（后端 env-builder、隔离 microvm），给它一条平台批准的授权
    （只放行部署登记的 build_hosts：镜像仓库与系统包的源），再签发出网与包镜像的凭据。出网网关于是按同一套规则放行构建
    拉基础镜像，用量与审计照记。构建结束（成功或失败）收回授权、记为销毁。"""

    def __init__(self, broker: SandboxBroker, hosts: tuple[str, ...]) -> None:
        if broker.ledger is None or broker.grants is None:
            raise ValueError("构建联网要有沙箱环境表")
        self.broker = broker
        self.hosts = tuple(sorted(set(hosts)))
        self._records: dict[tuple[str, str], EnvRecord] = {}

    @staticmethod
    def task_id(key: str) -> str:
        return f"envbuild-{key[:16]}"

    async def open(self, owner: str, key: str, timeout: timedelta) -> dict[str, str]:
        from agentruntime_worker.sandbox.grants import NetworkPolicy, NetworkRequest
        from agentruntime_worker.sandbox.ledger import Quota

        ledger, grants = self.broker.ledger, self.broker.grants
        assert ledger is not None and grants is not None
        lifetime = timeout + timedelta(minutes=10)
        task = self.task_id(key)
        record = await ledger.begin_create(
            owner, backend=BUILD_BACKEND, kind="k8s", isolation="microvm", task_id=task, writable=False, idle=lifetime, max_lifetime=lifetime,
        )
        record = await ledger.mark_ready(record, f"envb-{key[:16]}")
        self._records[(owner, key)] = record
        if self.hosts:
            minutes = int(lifetime.total_seconds() // 60) + 1
            await grants.create(owner, NetworkRequest(hosts=self.hosts, ports=(80, 443), minutes=minutes, reason="构建项目环境"),
                                NetworkPolicy(allowed=True, hosts=self.hosts), task_id=task, approved_by="platform:env-builder")
        if self.broker.credentials is None:
            return {}
        return await self.broker.credentials.exec_env(record, {"kind": "k8s"}, Quota())

    async def close(self, owner: str, key: str) -> None:
        record = self._records.pop((owner, key), None)
        if record is None and self.broker.ledger is not None:
            record = await self.broker.ledger.by_backend_env(owner, BUILD_BACKEND, f"envb-{key[:16]}")
        if record is not None and record.state == "ready":
            await self.broker._destroy_record(record, "build finished")  # noqa: SLF001


__all__ = ["BUILD_BACKEND", "BuildNetwork", "Lease", "SandboxBroker"]
