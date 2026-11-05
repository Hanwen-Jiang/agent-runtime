"""Sandbox Service 的客户端（SBX-01；docs/plan/sbx-01-sandbox-service-2026-11-03.md）。

Worker 的沙箱一律经 Sandbox Service（services/go-sandbox-service）：借还、选后端、配额、会话级复用、存档与恢复、冷热分层、
环境配置的准备、对账与按项目构建环境都在服务里；Worker 只剩工具的语义（shell_run 怎么截断输出、repo_edit 怎么改文件、
Workspace 操作跑哪些 git 命令）。没有按租户的开关（2026-11-04 用户纠正）：整个部署一起切，回退是回滚 Release。

- 认证：调用方令牌在 SecretStore 平台作用域的 sandbox-service/tokens/worker（AR_SECRETS_FILE），每个请求带
  authorization: Bearer <令牌> 与 x-agentruntime-caller: worker。
- 租户：每个请求带 TenantContext；服务按它绑定所有 Provider。
- 平台注入的环境变量（包镜像、出网代理、测试用密钥、.env）只在服务里，Exec 时由服务补上：RemoteLease.env 永远是空的。
- lease_id 是环境表的 env_ref：服务重启之后照常用同一个 lease。
- 错误按 ar-error-bin 尾部还原成 ProviderError，错误码原样保留（workspace_busy、sandbox_quota_exceeded 等）。

RemoteLease、RemoteBox 的形状与原来进程内的 Lease、Sandbox 一致（backend、env_id、record、box.exec 等），工具代码不用分两路。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import IO, Any

import grpc

from agentruntime.common.v1 import tenant_pb2
from agentruntime.environment.v1 import environment_pb2 as env_pb2
from agentruntime.sandbox.v1 import sandbox_pb2 as pb
from agentruntime.sandbox.v1 import sandbox_pb2_grpc as pb_grpc
from agentruntime_core.errors import ProviderError, TransientError
from agentruntime_core.providers.sandbox import DirEntry, ExecResult, FileEntry, GrepMatch, SandboxSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.executor.grpc_gateway import ERROR_TRAILER, _BY_STATUS, _from_trailer

CALLER = "worker"
TOKEN_SECRET = "sandbox-service/tokens/worker"
MAX_MESSAGE = 64 << 20
CHUNK = 1 << 20
# DescribeTenant 的结果按租户缓存多久（秒）：租户策略在服务里也按一分钟缓存。
DESCRIBE_TTL = 60.0


def restore(exc: grpc.aio.AioRpcError) -> ProviderError:
    for key, value in exc.trailing_metadata() or ():
        if key == ERROR_TRAILER:
            restored = _from_trailer(value)
            if restored is not None:
                return restored
            break
    status = exc.code()
    name = status.name.lower() if isinstance(status, grpc.StatusCode) else "unknown"
    cls = _BY_STATUS.get(status, TransientError)
    return cls(f"sandbox_service_{name}", f"调用 Sandbox Service 失败（gRPC {name}）")


def _tenant(t: TenantContext) -> tenant_pb2.TenantContext:
    return tenant_pb2.TenantContext(tenant_id=t.tenant_id, user_id=t.user_id, workspace_id=t.workspace_id, session_id=t.session_id)


@dataclass(frozen=True)
class RemoteBackend:
    name: str
    kind: str
    isolation: str


_STATES = {v.number: v.name.removeprefix("ENVIRONMENT_STATE_").lower() for v in env_pb2.EnvironmentState.DESCRIPTOR.values}


@dataclass(frozen=True)
class RecordView:
    """环境表的一行（只读的投影；Worker 不再直接读写环境表）。"""

    env_ref: str
    owner: str
    state: str
    workspace_id: str = ""
    session_id: str = ""
    task_id: str = ""
    runtime: str = ""
    image: str = ""
    writable: bool = False
    cost_usd: float = 0.0

    @classmethod
    def of(cls, lease: pb.Lease) -> RecordView:
        r = lease.record
        return cls(
            env_ref=r.env_ref or lease.lease_id, owner=r.tenant_id, state=_STATES.get(r.state, "unknown"), workspace_id=r.workspace_id,
            session_id=r.session_id, task_id=r.task_id, runtime=r.runtime, image=r.image, writable=r.writable, cost_usd=lease.cost_usd,
        )


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    size_bytes: int
    files: int


@dataclass
class RemoteLease:
    """一次借用（Sandbox Service 的 Lease）。env_id 就是 lease_id；env 永远是空的（凭据只在服务里）。"""

    backend: RemoteBackend
    box: RemoteBox
    env_id: str
    record: RecordView | None
    reused: bool = False
    fallback_from: str = ""
    fallback_reason: str = ""
    env: dict[str, str] = field(default_factory=dict, repr=False)
    network: str = "none"
    test_env_names: tuple[str, ...] = ()
    owner: str = ""
    workspace_id: str = ""
    archive: bool = False
    writable: bool = False
    restored: str = ""
    prepared: dict[str, str] | None = None
    backend_env_id: str = ""
    # 项目环境的解析结果（D-17），给 shell_run 的结果；没有时 None。
    environment: dict[str, Any] | None = None

    @classmethod
    def of(cls, msg: pb.Lease, box: RemoteBox) -> RemoteLease:
        prepared = None
        if msg.HasField("prepared"):
            prepared = {"environment_id": msg.prepared.environment_id, "setup": msg.prepared.setup, "start": msg.prepared.start}
        environment = None
        if msg.environment_json:
            try:
                environment = json.loads(msg.environment_json)
            except ValueError:
                environment = None
        return cls(
            backend=RemoteBackend(msg.backend.name, msg.backend.kind, msg.backend.isolation), box=box, env_id=msg.lease_id,
            record=RecordView.of(msg) if msg.HasField("record") else None, reused=msg.reused, fallback_from=msg.fallback_from,
            fallback_reason=msg.fallback_reason, network=msg.network or "none", test_env_names=tuple(msg.test_env_names), owner=box.tenant.tenant_id,
            workspace_id=msg.record.workspace_id, archive=msg.archive, writable=msg.writable, restored=msg.restored_snapshot, prepared=prepared,
            backend_env_id=msg.backend_env_id, environment=environment,
        )

    def update(self, msg: pb.Lease) -> None:
        if msg.HasField("record"):
            self.record = RecordView.of(msg)

    @property
    def attributes(self) -> dict[str, Any]:
        """给 runtime.sandbox span 的属性（不含命令、输出与凭据；测试用密钥只记名字）。"""
        out: dict[str, Any] = {"sandbox.backend": self.backend.name, "sandbox.kind": self.backend.kind, "sandbox.isolation": self.backend.isolation,
                               "sandbox.network": self.network, "sandbox.service": True}
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


class RemoteBox:
    """借到的环境：方法与原来的 Sandbox 一样（第一个参数是 env_id，就是 lease_id）。"""

    def __init__(self, client: ServiceBroker, tenant: TenantContext) -> None:
        self.client = client
        self.tenant = tenant

    async def exec(
        self, env_id: str, argv: Sequence[str], *, timeout: timedelta, stdin: bytes = b"", env: Mapping[str, str] | None = None
    ) -> ExecResult:
        req = pb.ExecRequest(tenant=_tenant(self.tenant), lease_id=env_id, argv=list(argv), timeout_ms=max(1, int(timeout.total_seconds() * 1000)),
                             stdin=stdin, env=dict(env or {}))
        resp = await self.client.call("Exec", req, timeout=timeout.total_seconds() + 120)
        return ExecResult(
            exit_code=resp.exit_code if resp.has_exit_code else None, stdout=resp.stdout, stderr=resp.stderr, timed_out=resp.timed_out,
            cpu_time=timedelta(milliseconds=resp.cpu_time_ms) if resp.cpu_time_ms >= 0 else None,
        )

    async def write_file(self, env_id: str, path: str, data: bytes) -> None:
        await self.client.call("WriteFile", pb.WriteFileRequest(tenant=_tenant(self.tenant), lease_id=env_id, path=path, data=data))

    async def read_file(self, env_id: str, path: str) -> bytes:
        resp = await self.client.call("ReadLeaseFile", pb.ReadLeaseFileRequest(tenant=_tenant(self.tenant), lease_id=env_id, path=path))
        return bytes(resp.data)

    async def list_dir(self, env_id: str, path: str = "") -> list[DirEntry]:
        resp = await self.client.call("ListLeaseDir", pb.ListLeaseDirRequest(tenant=_tenant(self.tenant), lease_id=env_id, path=path))
        return [DirEntry(name=e.name, type=e.type, size=e.size) for e in resp.entries]

    async def grep(self, env_id: str, pattern: str, *, path: str = "", max_matches: int = 100) -> tuple[list[GrepMatch], bool]:
        resp = await self.client.call("GrepLease", pb.GrepLeaseRequest(tenant=_tenant(self.tenant), lease_id=env_id, pattern=pattern, path=path,
                                                                       max_matches=max_matches), timeout=360)
        return [GrepMatch(path=m.path, line=m.line, text=m.text) for m in resp.matches], resp.truncated

    async def manifest(self, env_id: str) -> list[FileEntry]:
        resp = await self.client.call("Manifest", pb.ManifestRequest(tenant=_tenant(self.tenant), lease_id=env_id), timeout=660)
        return [FileEntry(path=e.path, type=e.type, mode=e.mode, size=e.size, sha256=e.sha256, link=e.link) for e in resp.entries]

    async def export_archive(self, env_id: str, out: IO[bytes], *, max_bytes: int) -> int:
        stub = self.client.stub()
        req = pb.ExportArchiveRequest(tenant=_tenant(self.tenant), lease_id=env_id, max_bytes=max_bytes)
        written = 0
        await self.client.ensure_token()
        try:
            async for chunk in stub.ExportArchive(req, metadata=self.client.metadata(), timeout=900):
                out.write(chunk.data)
                written += len(chunk.data)
        except grpc.aio.AioRpcError as exc:
            raise restore(exc) from None
        return written

    async def import_archive(self, env_id: str, src: IO[bytes], *, size: int, max_bytes: int) -> None:
        header = pb.ImportArchiveRequest(header=pb.ImportArchiveHeader(tenant=_tenant(self.tenant), lease_id=env_id, size=size, max_bytes=max_bytes))

        async def parts() -> AsyncIterator[pb.ImportArchiveRequest]:
            yield header
            while data := src.read(CHUNK):
                yield pb.ImportArchiveRequest(chunk=data)

        await self.client.ensure_token()
        try:
            await self.client.stub().ImportArchive(parts(), metadata=self.client.metadata(), timeout=900)
        except grpc.aio.AioRpcError as exc:
            raise restore(exc) from None

    async def preview_address(self, env_id: str, port: int) -> str:
        resp = await self.client.call("PreviewAddress", pb.PreviewAddressRequest(tenant=_tenant(self.tenant), lease_id=env_id, port=port))
        return str(resp.address)


@dataclass(frozen=True)
class TenantSummary:
    backends: tuple[str, ...]
    archive_mode: bool
    workspace_max_bytes: int
    max_workspaces: int
    gateway: bool
    kind: str
    isolation: str


class ServiceBroker:
    """Worker 用沙箱的唯一入口（替换原来进程内的 SandboxBroker）。archiver 是 Worker 这边读写工作区记录的
    WorkspaceArchiver（工作区的版本号、Workspace 操作的推送记录等；存档与恢复在服务里）。"""

    def __init__(self, address: str, token: str | Callable[[], Awaitable[str]], *, archiver: Any = None, timeout_s: float = 600.0) -> None:
        self.address = address
        # 令牌：直接给，或给一个第一次调用时读 SecretStore 的函数（Worker 在事件循环之外组装）。
        self._token = token if isinstance(token, str) else ""
        self._load_token = None if isinstance(token, str) else token
        self.archiver = archiver
        self.timeout_s = timeout_s
        self._channel: grpc.aio.Channel | None = None
        self._stub: pb_grpc.SandboxServiceStub | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._described: dict[str, tuple[float, TenantSummary]] = {}

    def __repr__(self) -> str:
        return f"ServiceBroker({self.address!r})"

    @classmethod
    def connect(cls, address: str, token: str, **kwargs: Any) -> ServiceBroker:
        return cls(address, token, **kwargs)

    def metadata(self) -> tuple[tuple[str, str], ...]:
        return (("authorization", f"Bearer {self._token}"), ("x-agentruntime-caller", CALLER))

    def stub(self) -> pb_grpc.SandboxServiceStub:
        loop = asyncio.get_running_loop()
        if self._stub is None or self._loop is not loop:
            options = [("grpc.max_receive_message_length", MAX_MESSAGE), ("grpc.max_send_message_length", MAX_MESSAGE)]
            self._channel = grpc.aio.insecure_channel(self.address, options=options)
            self._stub = pb_grpc.SandboxServiceStub(self._channel)
            self._loop = loop
        return self._stub

    async def ensure_token(self) -> None:
        if not self._token and self._load_token is not None:
            self._token = await self._load_token()

    async def call(self, method: str, request: Any, *, timeout: float | None = None) -> Any:
        await self.ensure_token()
        try:
            return await getattr(self.stub(), method)(request, metadata=self.metadata(), timeout=timeout or self.timeout_s)
        except grpc.aio.AioRpcError as exc:
            raise restore(exc) from None

    async def aclose(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = self._stub = None

    # --- 借还 ---

    async def acquire(
        self, tenant: TenantContext, spec: SandboxSpec, *, task_id: str = "", writable: bool = True, reuse: bool = False,
        busy_for: timedelta | None = None, prepare: bool = False, cooling: bool = False, project_environment: bool = False,
        flush_writable: bool = False,
    ) -> RemoteLease:
        """flush_writable：存档模式下先把这个工作区别人闲着的可写环境各存一份快照（不销毁），有命令在跑时 workspace_busy。"""
        req = pb.AcquireRequest(
            tenant=_tenant(tenant), task_id=task_id, writable=writable, reuse=reuse, prepare=prepare, cooling=cooling,
            project_environment=project_environment, flush_writable=flush_writable, busy_for_ms=int(busy_for.total_seconds() * 1000) if busy_for is not None else 0,
            spec=pb.Spec(runtime=spec.runtime, cpu_millis=spec.cpu_millis, memory_mb=spec.memory_mb, idle_timeout_s=int(spec.idle_timeout.total_seconds()),
                         workspace_id=spec.workspace_id, image=spec.image),
        )
        resp = await self.call("Acquire", req, timeout=max(self.timeout_s, 900))
        return RemoteLease.of(resp.lease, RemoteBox(self, tenant))

    async def release(self, lease: RemoteLease, *, result: ExecResult | None = None, keep: bool = False, skip_snapshot: bool = False) -> None:
        """skip_snapshot：销毁之前不存档（操作失败时不存半截的改动；刚存过的不再存）。"""
        cpu_ms = int(result.cpu_time.total_seconds() * 1000) if result is not None and result.cpu_time is not None else 0
        resp = await self.call("Release", pb.ReleaseRequest(tenant=_tenant(lease.box.tenant), lease_id=lease.env_id, keep=keep, cpu_time_ms=cpu_ms,
                                                            skip_snapshot=skip_snapshot))
        lease.update(resp.lease)

    async def snapshot(self, lease: RemoteLease) -> Snapshot:
        resp = await self.call("Snapshot", pb.SnapshotRequest(tenant=_tenant(lease.box.tenant), lease_id=lease.env_id), timeout=1800)
        return Snapshot(snapshot_id=resp.snapshot_id, size_bytes=resp.bytes, files=resp.files)

    async def discard(self, lease: RemoteLease, reason: str) -> None:
        await self.call("Discard", pb.DiscardRequest(tenant=_tenant(lease.box.tenant), lease_id=lease.env_id, reason=reason[:300]))

    async def destroy_session(self, tenant_id: str, *, workspace_id: str, session_id: str) -> int:
        resp = await self.call("DestroySession", pb.DestroySessionRequest(tenant=tenant_pb2.TenantContext(tenant_id=tenant_id), workspace_id=workspace_id,
                                                                        session_id=session_id))
        return int(resp.destroyed)

    async def destroy_workspace(self, tenant_id: str, workspace_id: str) -> int:
        resp = await self.call("DestroyWorkspace", pb.DestroyWorkspaceRequest(tenant=tenant_pb2.TenantContext(tenant_id=tenant_id), workspace_id=workspace_id))
        return int(resp.destroyed)

    async def destroy_tenant(self, tenant_id: str) -> int:
        resp = await self.call("DestroyTenant", pb.DestroyTenantRequest(tenant=tenant_pb2.TenantContext(tenant_id=tenant_id)))
        return int(resp.destroyed)

    # --- 租户的沙箱策略摘要 ---

    async def describe(self, tenant_id: str) -> TenantSummary:
        cached = self._described.get(tenant_id)
        if cached is not None and time.monotonic() - cached[0] < DESCRIBE_TTL:
            return cached[1]
        resp = await self.call("DescribeTenant", pb.DescribeTenantRequest(tenant=tenant_pb2.TenantContext(tenant_id=tenant_id)))
        summary = TenantSummary(
            backends=tuple(resp.backends), archive_mode=resp.archive_mode, workspace_max_bytes=resp.workspace_max_bytes,
            max_workspaces=resp.max_workspaces, gateway=resp.gateway, kind=resp.kind, isolation=resp.isolation,
        )
        self._described[tenant_id] = (time.monotonic(), summary)
        return summary

    async def archive_mode_for(self, tenant_id: str) -> bool:
        return (await self.describe(tenant_id)).archive_mode

    async def workspace_max_bytes(self, tenant_id: str) -> int:
        return (await self.describe(tenant_id)).workspace_max_bytes


class ServiceSandboxProvider:
    """Sandbox Provider 的形状（bind → create、exec、write_file、read_file、destroy），环境由 Sandbox Service 借：
    Node Executor 的内置模板 sandbox_exec 用它（每次 create 借一个一次性的环境，destroy 还掉）。"""

    def __init__(self, broker: ServiceBroker) -> None:
        self.broker = broker

    def bind(self, tenant: TenantContext) -> _ServiceSandbox:
        return _ServiceSandbox(self.broker, tenant)


class _ServiceSandbox:
    def __init__(self, broker: ServiceBroker, tenant: TenantContext) -> None:
        self.broker = broker
        self.tenant = tenant
        self._leases: dict[str, RemoteLease] = {}

    async def create(self, spec: SandboxSpec) -> Any:
        from agentruntime_core.providers.sandbox import SandboxEnv

        lease = await self.broker.acquire(self.tenant, spec, writable=bool(spec.workspace_id))
        self._leases[lease.env_id] = lease
        return SandboxEnv(env_id=lease.env_id, spec=spec, image=lease.record.image if lease.record else "")

    async def exec(
        self, env_id: str, argv: Sequence[str], *, timeout: timedelta, stdin: bytes = b"", env: Mapping[str, str] | None = None
    ) -> ExecResult:
        return await RemoteBox(self.broker, self.tenant).exec(env_id, argv, timeout=timeout, stdin=stdin, env=env)

    async def write_file(self, env_id: str, path: str, data: bytes) -> None:
        await RemoteBox(self.broker, self.tenant).write_file(env_id, path, data)

    async def read_file(self, env_id: str, path: str) -> bytes:
        return await RemoteBox(self.broker, self.tenant).read_file(env_id, path)

    async def destroy(self, env_id: str) -> None:
        lease = self._leases.pop(env_id, None)
        if lease is None:
            lease = RemoteLease(backend=RemoteBackend("", "", ""), box=RemoteBox(self.broker, self.tenant), env_id=env_id, record=None)
        from agentruntime_core.errors import NotFoundError

        try:
            await self.broker.release(lease)
        except NotFoundError:
            return


def secrets_token(secrets_file: Any) -> Callable[[], Awaitable[str]]:
    """第一次调用时读调用方令牌（SecretStore 平台作用域的 sandbox-service/tokens/worker）；没有时报 invalid。"""

    async def read() -> str:
        from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
        from agentruntime_core.errors import InvalidError, NotFoundError

        try:
            value = (await LocalFileSecretStoreProvider(secrets_file).bind_platform("sandbox").read(TOKEN_SECRET)).reveal()
        except NotFoundError:
            raise InvalidError("missing_sandbox_service_token", f"SecretStore 的平台作用域里没有 {TOKEN_SECRET}") from None
        return value.decode() if isinstance(value, bytes) else str(value)

    return read


__all__ = ["CALLER", "TOKEN_SECRET", "RemoteBackend", "RemoteBox", "RemoteLease", "ServiceBroker", "ServiceSandboxProvider", "Snapshot", "TenantSummary", "secrets_token"]
