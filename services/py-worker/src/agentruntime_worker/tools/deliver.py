"""deliver_file：把工作区里的文件作为 Task 的交付（N-18，docs/plan/n18-file-delivery-f3.md，F3）。

- effect 为 write_reversible（对象会过期、可以删），要幂等键（附录 D.4）。同一个幂等键只写一次：先查业务库里的记录，
  Worker 重启、进程内的幂等记录没了也一样。
- 读文件经读取接口（DirectoryReader：<工作区根>/<租户>/<workspace，没有时为 default>，与 repo_* 同一处；存档模式的后端以后另接）。
- 路径是规范化的 POSIX 相对路径：拒绝绝对路径、空段、. 与 ..、反斜杠、控制字符、.git 等保留目录、逃出工作区的符号链接。
- 上限（单文件、每个 Task 的个数与总字节数）只能由部署收窄（AR_DELIVERY_MAX_FILE_BYTES、AR_DELIVERY_MAX_FILES、
  AR_DELIVERY_MAX_TASK_BYTES，大于硬上限的按硬上限）；超限时不写对象也不写记录。
- 内容写进 ObjectStore（租户作用域，deliveries/<task>/<随机>/<名字>），元数据写进业务库的 task_artifacts（迁移 209，租户作用域；
  task-service 只读，ListTaskArtifacts 与下载路由用它）。保留 7 天（RETENTION），到期由 ObjectStore 的生命周期规则清掉。
- 工具的输出与日志里没有对象地址与文件内容。
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from agentruntime_core.clock import Clock, SystemClock
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.enums import Effect
from agentruntime_core.errors import InvalidError, NotFoundError
from agentruntime_core.providers.object_store import ObjectStoreProvider
from agentruntime_core.providers.relational_store import Migration, RelationalStoreProvider
from agentruntime_core.providers.tool_driver import ToolInvocation, ToolSpec
from agentruntime_core.tenancy import TenantContext

_LOG = logging.getLogger(__name__)

TOOL = "deliver_file"
VERSION = "v1"
RETENTION = timedelta(days=7)
PREFIX = "deliveries"
_RESERVED = frozenset({".git", ".hg", ".svn"})
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# 常见交付文件的媒体类型；其余按 mimetypes，猜不出时 application/octet-stream。下载时 task-service 另有允许列表。
_MEDIA = {
    ".txt": "text/plain", ".log": "text/plain", ".md": "text/markdown", ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".json": "application/json", ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".zip": "application/zip", ".gz": "application/gzip", ".tgz": "application/gzip",
    ".tar": "application/x-tar", ".html": "text/html", ".htm": "text/html", ".svg": "image/svg+xml",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

MIGRATIONS = (
    Migration(209, "task_artifacts", (
        """CREATE TABLE task_artifacts (
            artifact_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            media_type TEXT NOT NULL,
            size_bytes BIGINT NOT NULL,
            sha256 TEXT NOT NULL,
            object_uri TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )""",
    )),
    Migration(214, "task_artifact_reservations", (
        "CREATE TABLE task_artifact_quota_lock (task_id TEXT PRIMARY KEY, version BIGINT NOT NULL DEFAULT 0)",
        "CREATE TABLE task_artifact_reservations (idempotency_key TEXT PRIMARY KEY, reservation_id TEXT NOT NULL, task_id TEXT NOT NULL, size_bytes BIGINT NOT NULL, expires_at TEXT NOT NULL)",
        "CREATE INDEX task_artifact_reservations_task ON task_artifact_reservations (task_id, expires_at)",
    )),
)


@dataclass(frozen=True)
class DeliveryLimits:
    max_file_bytes: int
    max_files_per_task: int
    max_task_bytes: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> DeliveryLimits:
        """部署配置只能收窄硬上限；不是正整数时报错。"""

        def pick(name: str, hard: int) -> int:
            raw = str(env.get(name) or "").strip()
            if not raw:
                return hard
            try:
                value = int(raw)
            except ValueError:
                raise ValueError(f"{name} 要是正整数") from None
            if value <= 0:
                raise ValueError(f"{name} 要是正整数")
            return min(value, hard)

        return cls(pick("AR_DELIVERY_MAX_FILE_BYTES", HARD_LIMITS.max_file_bytes), pick("AR_DELIVERY_MAX_FILES", HARD_LIMITS.max_files_per_task),
                   pick("AR_DELIVERY_MAX_TASK_BYTES", HARD_LIMITS.max_task_bytes))


HARD_LIMITS = DeliveryLimits(max_file_bytes=25 * 2**20, max_files_per_task=20, max_task_bytes=100 * 2**20)

SPEC = ToolSpec(
    name=TOOL,
    version=VERSION,
    description="把工作区里的一个文件交付给用户（报告、表格、图、打好的包）：用户在交付区看到并下载。路径相对工作区；可选显示名。",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 1024}, "name": {"type": "string", "minLength": 1, "maxLength": 255}},
        "required": ["path"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {k: {"type": t} for k, t in (("artifact_id", "string"), ("name", "string"), ("path", "string"), ("media_type", "string"),
                                                   ("size_bytes", "integer"), ("sha256", "string"), ("expires_at", "string"))},
    },
    effect=Effect.WRITE_REVERSIBLE,
    permissions=frozenset(),
    timeout=timedelta(minutes=2),
)


class WorkspaceReader(Protocol):
    async def read(self, tenant_id: str, workspace_id: str, path: str, *, max_bytes: int) -> bytes: ...


class DirectoryReader:
    """工作区在本机目录里（host、docker 与挂工作区卷的沙箱后端，与 repo_* 同一处）。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    async def read(self, tenant_id: str, workspace_id: str, path: str, *, max_bytes: int) -> bytes:
        workspace = (self.root / tenant_id / (workspace_id or "default")).resolve()
        target = workspace.joinpath(*PurePosixPath(path).parts).resolve()
        if not target.is_relative_to(workspace) or target == workspace:
            raise InvalidError("path_outside_workspace", f"路径 {path} 不在工作区里")
        if not target.is_file():
            raise NotFoundError("file_not_found", f"工作区里没有文件 {path}")
        if target.stat().st_size > max_bytes:
            raise InvalidError("delivery_file_too_large", f"文件超过上限 {max_bytes} 字节")
        data = target.read_bytes()
        if len(data) > max_bytes:
            raise InvalidError("delivery_file_too_large", f"文件超过上限 {max_bytes} 字节")
        return data


def clean_path(path: Any) -> str:
    if not isinstance(path, str) or not path or len(path) > 1024:
        raise InvalidError("invalid_delivery_path", "路径要是 1 到 1024 个字符")
    if path.startswith("/") or "\\" in path or any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        raise InvalidError("invalid_delivery_path", "路径要相对工作区，用 / 分隔，不含控制字符")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts) or (parts and ":" in parts[0]):
        raise InvalidError("invalid_delivery_path", f"路径 {path} 不合规（空段、. 或 ..）")
    if any(p in _RESERVED for p in parts):
        raise InvalidError("invalid_delivery_path", f"路径 {path} 在保留目录里")
    return path


def _name(value: str) -> str:
    if not value or len(value) > 255 or value in {".", ".."} or "/" in value or "\\" in value or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise InvalidError("invalid_delivery_name", "显示名要是 1 到 255 个字符的单个文件名")
    return value


def media_type(name: str) -> str:
    suffix = PurePosixPath(name).suffix.lower()
    return _MEDIA.get(suffix) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def _artifact_id(now: datetime) -> str:
    n = (int(now.timestamp() * 1000) << 80) | secrets.randbits(80)
    return "art_" + "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in reversed(range(26)))


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _output(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"artifact_id": row["artifact_id"], "name": row["name"], "path": row["path"], "media_type": row["media_type"],
            "size_bytes": int(row["size_bytes"]), "sha256": row["sha256"], "expires_at": row["expires_at"]}


class DeliverFile:
    def __init__(self, *, objects: ObjectStoreProvider, store: RelationalStoreProvider, reader: WorkspaceReader, limits: DeliveryLimits = HARD_LIMITS,
                 clock: Clock | None = None) -> None:
        self.objects, self.store, self.reader, self.limits = objects, store, reader, limits
        self.clock = clock or SystemClock()
        self._migrated = False

    async def _existing(self, tenant: TenantContext, key: str) -> dict[str, Any] | None:
        async with self.store.bind(tenant).transaction(read_only=True) as tx:
            row = await tx.query_one("SELECT * FROM task_artifacts WHERE idempotency_key = $1", (key,))
        return dict(row) if row else None

    async def _reserve(self, tenant: TenantContext, invocation: ToolInvocation, reservation: str, size: int) -> None:
        if size < 0 or size > self.limits.max_file_bytes:
            raise InvalidError("delivery_file_too_large", f"文件超过上限 {self.limits.max_file_bytes} 字节")
        now = _iso(self.clock.now())
        async with self.store.bind(tenant).transaction() as tx:
            await tx.execute("INSERT INTO task_artifact_quota_lock (task_id, version) VALUES ($1, 1) ON CONFLICT (task_id) DO UPDATE SET version = task_artifact_quota_lock.version + 1", (invocation.task_id,))
            existing = await tx.query_one("SELECT artifact_id FROM task_artifacts WHERE idempotency_key = $1", (invocation.idempotency_key,))
            if existing:
                raise InvalidError("delivery_already_committed", "交付已完成")
            pending = await tx.query_one("SELECT reservation_id, expires_at FROM task_artifact_reservations WHERE idempotency_key = $1", (invocation.idempotency_key,))
            if pending and str(pending["expires_at"]) > now:
                raise InvalidError("delivery_pending", "相同交付正在上传，请稍后重试")
            if pending:
                await tx.execute("DELETE FROM task_artifact_reservations WHERE idempotency_key = $1", (invocation.idempotency_key,))
            used = await tx.query_one("SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS total FROM task_artifacts WHERE task_id=$1", (invocation.task_id,))
            reserved = await tx.query_one("SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS total FROM task_artifact_reservations WHERE task_id=$1 AND expires_at>$2", (invocation.task_id, now))
            count = int(used["n"] if used else 0) + int(reserved["n"] if reserved else 0)
            total = int(used["total"] if used else 0) + int(reserved["total"] if reserved else 0)
            if count + 1 > self.limits.max_files_per_task:
                raise InvalidError("delivery_too_many_files", f"一个 Task 最多交付 {self.limits.max_files_per_task} 个文件")
            if total + size > self.limits.max_task_bytes:
                raise InvalidError("delivery_task_too_large", f"一个 Task 交付的文件合计不超过 {self.limits.max_task_bytes} 字节")
            await tx.execute("INSERT INTO task_artifact_reservations (idempotency_key,reservation_id,task_id,size_bytes,expires_at) VALUES ($1,$2,$3,$4,$5)",
                             (invocation.idempotency_key, reservation, invocation.task_id, size, _iso(self.clock.now() + timedelta(minutes=5))))

    async def _unreserve(self, tenant: TenantContext, key: str, reservation: str) -> None:
        async with self.store.bind(tenant).transaction() as tx:
            await tx.execute("DELETE FROM task_artifact_reservations WHERE idempotency_key=$1 AND reservation_id=$2", (key, reservation))

    async def __call__(self, invocation: ToolInvocation) -> Any:
        if not invocation.task_id:
            raise InvalidError("delivery_needs_task", "交付文件要在 Task 里调用")
        if not invocation.idempotency_key:
            raise InvalidError("missing_idempotency_key", "写操作必须带幂等键")
        if not self._migrated:
            await self.store.migrate(MIGRATIONS)
            self._migrated = True
        tenant = TenantContext(tenant_id=invocation.tenant_id)
        key = invocation.idempotency_key
        if (row := await self._existing(tenant, key)) is not None:
            return _output(row)
        path = clean_path(invocation.arguments.get("path"))
        name = _name(str(invocation.arguments.get("name") or PurePosixPath(path).name))
        now = self.clock.now()
        artifact_id = _artifact_id(now)
        kind = media_type(name)
        objects = self.objects.bind(tenant)
        object_key: str | None = None
        reserved_size: int | None = None

        async def reserve(size: int) -> None:
            nonlocal reserved_size
            await self._reserve(tenant, invocation, artifact_id, size)
            reserved_size = size

        try:
            direct = getattr(self.reader, "upload_invocation", None)
            ref = await direct(invocation, path, name, reserve) if direct else None
            if ref is None:
                by_call = getattr(self.reader, "read_invocation", None)
                data = await by_call(invocation, path, max_bytes=self.limits.max_file_bytes) if by_call else await self.reader.read(invocation.tenant_id, invocation.workspace_id, path, max_bytes=self.limits.max_file_bytes)
                await reserve(len(data))
                object_key = f"{PREFIX}/{invocation.task_id}/{secrets.token_urlsafe(18)}/{name}"
                info = await objects.put(object_key, data, content_type=kind, metadata={"task_id": invocation.task_id, "artifact_id": artifact_id}, if_absent=True)
                ref = {"object_key": object_key, "object_uri": info.uri, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            object_key = str(ref["object_key"])
            # Never trust a service response to change this task's object scope.
            if objects.key_of(str(ref["object_uri"])) != object_key or not object_key.startswith(f"{PREFIX}/{invocation.task_id}/"):
                object_key = None
                raise InvalidError("delivery_object_mismatch", "交付对象作用域不匹配")
            if reserved_size is None or int(ref["size_bytes"]) != reserved_size:
                raise InvalidError("delivery_size_mismatch", "交付大小与预占配额不一致")
            row = {
                "artifact_id": artifact_id, "task_id": invocation.task_id, "idempotency_key": key, "name": name, "path": path, "media_type": kind,
                "size_bytes": reserved_size, "sha256": str(ref["sha256"]), "object_uri": str(ref["object_uri"]),
                "created_at": _iso(now), "expires_at": _iso(now + RETENTION),
            }
            async with self.store.bind(tenant).transaction() as tx:
                await tx.execute("INSERT INTO task_artifact_quota_lock (task_id,version) VALUES ($1,1) ON CONFLICT (task_id) DO UPDATE SET version=task_artifact_quota_lock.version+1", (invocation.task_id,))
                pending = await tx.query_one("SELECT reservation_id, expires_at FROM task_artifact_reservations WHERE idempotency_key=$1", (key,))
                if not pending or pending["reservation_id"] != artifact_id or str(pending["expires_at"]) <= _iso(self.clock.now()):
                    raise InvalidError("delivery_reservation_expired", "上传配额预占已过期，请重试")
                await tx.execute(
                    "INSERT INTO task_artifacts (artifact_id, task_id, idempotency_key, name, path, media_type, size_bytes, sha256, object_uri, created_at, expires_at)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                    tuple(row[k] for k in ("artifact_id", "task_id", "idempotency_key", "name", "path", "media_type", "size_bytes", "sha256",
                                           "object_uri", "created_at", "expires_at")),
                )
                await tx.execute("DELETE FROM task_artifact_reservations WHERE idempotency_key=$1 AND reservation_id=$2", (key, artifact_id))
        except BaseException as error:
            # A failed commit acknowledgement does not prove the transaction rolled
            # back. Resolve it before deleting bytes that may already be registered.
            try:
                existing = await self._existing(tenant, key)
            except Exception:
                _LOG.warning("delivery outcome unknown; retaining object and reservation")
                raise error from None
            registered_here = existing is not None and object_key is not None and existing["object_uri"] == objects.uri(object_key)
            cleanup_ok = True
            if object_key is not None and not registered_here:
                try:
                    await objects.delete(object_key)
                except Exception:
                    cleanup_ok = False
                    _LOG.warning("delivery cleanup failed; retaining reservation until expiry")
            if cleanup_ok:
                try:
                    await self._unreserve(tenant, key, artifact_id)
                except Exception:
                    _LOG.warning("delivery reservation cleanup failed; expiry will release it")
            if isinstance(error, Exception) and existing is not None:
                return _output(existing)
            raise
        return _output(row)


def register_delivery_tools(provider: LocalToolDriverProvider, *, objects: ObjectStoreProvider, store: RelationalStoreProvider, reader: WorkspaceReader,
                            limits: DeliveryLimits = HARD_LIMITS, clock: Clock | None = None) -> None:
    provider.register(SPEC, DeliverFile(objects=objects, store=store, reader=reader, limits=limits, clock=clock))


__all__ = ["HARD_LIMITS", "MIGRATIONS", "RETENTION", "SPEC", "DeliverFile", "DeliveryLimits", "DirectoryReader", "WorkspaceReader", "clean_path",
           "media_type", "register_delivery_tools"]
