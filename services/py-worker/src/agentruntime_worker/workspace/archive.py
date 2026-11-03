"""工作区的存档与恢复（设计书改动稿 W2；D-06c，docs/plan/workspace-layer.md 第 2、3 节）。

不挂工作区卷的后端（workspace: archive）靠这里把工作区搬进搬出环境：

- 存档（snapshot）：从环境导出 tar（SandboxArchive.export_archive，先写临时文件），逐个文件按 sha256 存成块
  （ObjectStore 租户作用域的 workspaces/<ws>/blobs/<sha256>，已有的不再写），再写清单（workspaces/<ws>/snapshots/<id>.json），
  记一行快照；只留最近 keep 份，没人引用的块删掉。导出的 tar 来自沙箱，不可信：名字、链接、类型与总大小在这里再核对一遍。
- 恢复（restore）：按清单从块拼出 tar（临时文件，块的内容逐个核对 sha256），导入一个空的环境，再取环境的清单逐项核对
  （路径、类型、大小、sha256、链接；权限位只恢复、不核对：Windows 上的开发实现表示不了）。不一致是 transient
  （workspace_restore_mismatch），由 SandboxBroker 换一个环境再试一次。
- 业务库（迁移 205，租户作用域）：workspaces、workspace_snapshots、workspace_pushes（推送的 gate 与幂等记录，见 operations）。

同一个工作区同一时间只有一个可写的环境（沙箱环境表的规则），所以同一个工作区的存档与清理不会并发。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import secrets
import tarfile
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from agentruntime_core.dev._common import PLATFORM_ID
from agentruntime_core.dev.workdir import clean_name, link_inside
from agentruntime_core.errors import ConflictError, DeniedError, InvalidError, NotFoundError, TransientError
from agentruntime_core.providers.object_store import ObjectStore, ObjectStoreProvider
from agentruntime_core.providers.relational_store import Migration, RelationalStore, RelationalStoreProvider
from agentruntime_core.providers.sandbox import FileEntry
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.env_config import BoundEnvironment, EnvironmentConfig
from agentruntime_worker.sandbox.ledger import iso, utc_now
from agentruntime_worker.sandbox.policy import WorkspaceLimits

KEEP = 3
MANIFEST_VERSION = 1

MIGRATIONS = (
    Migration(205, "workspaces", (
        """CREATE TABLE workspaces (
            workspace_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            repo_url TEXT NOT NULL DEFAULT '',
            head TEXT NOT NULL DEFAULT '',
            snapshot_id TEXT NOT NULL DEFAULT '',
            size_bytes BIGINT NOT NULL DEFAULT 0,
            files BIGINT NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT NOT NULL DEFAULT ''
        )""",
        """CREATE TABLE workspace_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            seq BIGINT NOT NULL,
            manifest_key TEXT NOT NULL,
            files BIGINT NOT NULL DEFAULT 0,
            size_bytes BIGINT NOT NULL DEFAULT 0,
            new_blobs BIGINT NOT NULL DEFAULT 0,
            env_ref TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            deleted_at TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX workspace_snapshots_ws ON workspace_snapshots (workspace_id, seq)",
        """CREATE TABLE workspace_pushes (
            push_key TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            branch TEXT NOT NULL,
            sha TEXT NOT NULL,
            remote TEXT NOT NULL DEFAULT '',
            approved_by TEXT NOT NULL DEFAULT '',
            operation_id TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT ''
        )""",
    )),
    # ENV-02：工作区选用的环境配置的快照（只有普通值与密钥的名字）、选它的用户、删除环境配置的时间（Q35）。
    Migration(208, "workspace_environment", (
        "ALTER TABLE workspaces ADD COLUMN environment_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE workspaces ADD COLUMN environment TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE workspaces ADD COLUMN environment_owner TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE workspaces ADD COLUMN environment_deleted_at TEXT NOT NULL DEFAULT ''",
        "CREATE INDEX workspaces_environment ON workspaces (environment_id, environment_owner)",
    )),
)


def blob_key(workspace_id: str, sha256: str) -> str:
    return f"workspaces/{workspace_id}/blobs/{sha256}"


def manifest_key(workspace_id: str, snapshot_id: str) -> str:
    return f"workspaces/{workspace_id}/snapshots/{snapshot_id}.json"


def new_snapshot_id() -> str:
    return "wss-" + secrets.token_hex(10)


@dataclass(frozen=True)
class WorkspaceRecord:
    workspace_id: str
    state: str
    created_at: str
    updated_at: str
    repo_url: str = ""
    head: str = ""
    snapshot_id: str = ""
    size_bytes: int = 0
    files: int = 0
    deleted_at: str = ""
    # 选用的环境配置的 ID（ENV-02）；配置本身另读（environment）。
    environment_id: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> WorkspaceRecord:
        return cls(**{f.name: row[f.name] for f in fields(cls) if f.name in row})


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    workspace_id: str
    manifest_key: str
    created_at: str
    # 同一个工作区里的序号（新的大）：排序不靠时间（时钟可能相同或回拨）。
    seq: int = 0
    files: int = 0
    size_bytes: int = 0
    new_blobs: int = 0
    env_ref: str = ""
    deleted_at: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Snapshot:
        return cls(**{f.name: row[f.name] for f in fields(cls) if f.name in row})


_WS_COLUMNS = [f.name for f in fields(WorkspaceRecord)]
_SNAP_COLUMNS = [f.name for f in fields(Snapshot)]


def shape(entries: list[FileEntry]) -> list[tuple[str, str, int, str, str]]:
    """核对恢复结果用：路径、类型、大小、内容、链接（权限位只恢复、不核对）。"""
    return sorted((e.path, e.type, e.size if e.type == "file" else 0, e.sha256, e.link) for e in entries)


class WorkspaceArchiver:
    def __init__(
        self,
        store: RelationalStoreProvider,
        objects: ObjectStoreProvider,
        *,
        keep: int = KEEP,
        clock: Callable[[], datetime] = utc_now,
        tmp_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._objects = objects
        self.keep = max(1, keep)
        self._clock = clock
        self._tmp_dir = tmp_dir
        self._migrated = False

    async def migrate(self) -> None:
        if not self._migrated:
            await self._store.migrate(MIGRATIONS)
            self._migrated = True

    def _db(self, owner: str) -> RelationalStore:
        if owner == PLATFORM_ID or not owner:
            raise InvalidError("unsupported_workspace", "平台作用域没有工作区")
        return self._store.bind(TenantContext(tenant_id=owner))

    def _bucket(self, owner: str) -> ObjectStore:
        if owner == PLATFORM_ID or not owner:
            raise InvalidError("unsupported_workspace", "平台作用域没有工作区")
        return self._objects.bind(TenantContext(tenant_id=owner))

    # --- 工作区的行 ---

    async def get(self, owner: str, workspace_id: str) -> WorkspaceRecord | None:
        await self.migrate()
        async with self._db(owner).transaction(read_only=True) as tx:
            row = await tx.query_one(f"SELECT {', '.join(_WS_COLUMNS)} FROM workspaces WHERE workspace_id = $1", (workspace_id,))
        return WorkspaceRecord.from_row(row) if row else None

    async def list_workspaces(self, owner: str) -> list[WorkspaceRecord]:
        await self.migrate()
        async with self._db(owner).transaction(read_only=True) as tx:
            rows = await tx.query(f"SELECT {', '.join(_WS_COLUMNS)} FROM workspaces WHERE state <> 'deleted' ORDER BY workspace_id")
        return [WorkspaceRecord.from_row(r) for r in rows]

    async def ensure(self, owner: str, workspace_id: str, limits: WorkspaceLimits, *, repo_url: str = "", exclusive: bool = False) -> WorkspaceRecord:
        """工作区有一行（active）：没有时（或已删除）按 max_workspaces 核对后补一行。exclusive 时已有的（没删除的）报 conflict
        （Workspace API 的建）。"""
        await self.migrate()
        now = iso(self._clock())
        async with self._db(owner).transaction() as tx:
            row = await tx.query_one(f"SELECT {', '.join(_WS_COLUMNS)} FROM workspaces WHERE workspace_id = $1", (workspace_id,))
            if row and row["state"] != "deleted":
                if exclusive:
                    raise ConflictError("workspace_exists", f"已经有工作区 {workspace_id}")
                return WorkspaceRecord.from_row(row)
            count = await tx.query_one("SELECT COUNT(*) AS n FROM workspaces WHERE state <> 'deleted'")
            if limits.max_workspaces and int((count or {}).get("n") or 0) >= limits.max_workspaces:
                raise DeniedError("workspace_quota_exceeded", f"同时最多 {limits.max_workspaces} 个工作区，先删掉不用的")
            record = WorkspaceRecord(workspace_id=workspace_id, state="active", created_at=now, updated_at=now, repo_url=repo_url)
            if row:
                await tx.execute(
                    "UPDATE workspaces SET state = 'active', repo_url = $1, head = '', snapshot_id = '', size_bytes = 0, files = 0, "
                    "created_at = $2, updated_at = $2, deleted_at = '', environment_id = '', environment = '', environment_owner = '', "
                    "environment_deleted_at = '' WHERE workspace_id = $3",
                    (repo_url, now, workspace_id),
                )
            else:
                await tx.execute(
                    f"INSERT INTO workspaces ({', '.join(_WS_COLUMNS)}) VALUES ({', '.join(f'${i}' for i in range(1, len(_WS_COLUMNS) + 1))})",
                    [getattr(record, c) for c in _WS_COLUMNS],
                )
        return record

    async def update(self, owner: str, workspace_id: str, **values: Any) -> None:
        """改工作区的 head、repo_url 等（Workspace API 的操作之后）。"""
        allowed = {"head", "repo_url"}
        columns = [c for c in values if c in allowed]
        if not columns:
            return
        await self.migrate()
        sets = ", ".join(f"{c} = ${i}" for i, c in enumerate(columns, start=1))
        async with self._db(owner).transaction() as tx:
            await tx.execute(
                f"UPDATE workspaces SET {sets}, updated_at = ${len(columns) + 1} WHERE workspace_id = ${len(columns) + 2}",
                [*[values[c] for c in columns], iso(self._clock()), workspace_id],
            )

    # --- 选用的环境配置（ENV-02）---

    async def set_environment(self, owner: str, workspace_id: str, config: EnvironmentConfig, *, owner_user: str) -> None:
        """记下工作区选用的环境配置（建工作区时）；之后编辑环境只影响新建的工作区。"""
        await self.migrate()
        async with self._db(owner).transaction() as tx:
            await tx.execute(
                "UPDATE workspaces SET environment_id = $1, environment = $2, environment_owner = $3, environment_deleted_at = '', updated_at = $4 "
                "WHERE workspace_id = $5",
                (config.environment_id, config.to_json(), owner_user, iso(self._clock()), workspace_id),
            )

    async def environment(self, owner: str, workspace_id: str) -> BoundEnvironment | None:
        """工作区选用的环境配置；没有选用（或工作区不在）时 None。"""
        if owner == PLATFORM_ID or not owner:
            return None
        await self.migrate()
        async with self._db(owner).transaction(read_only=True) as tx:
            row = await tx.query_one(
                "SELECT environment, environment_owner, environment_deleted_at FROM workspaces WHERE workspace_id = $1 AND state <> 'deleted'",
                (workspace_id,),
            )
        if not row or not row["environment"]:
            return None
        return BoundEnvironment(EnvironmentConfig.from_json(str(row["environment"])), str(row["environment_owner"]), bool(row["environment_deleted_at"]))

    async def delete_environment(self, owner: str, environment_id: str, *, owner_user: str) -> int:
        """删除环境配置（Q35；task-service 的 DeleteEnvironmentConfig 做同样的事）：只标记，返回新标记的工作区个数。"""
        await self.migrate()
        now = iso(self._clock())
        async with self._db(owner).transaction() as tx:
            return await tx.execute(
                "UPDATE workspaces SET environment_deleted_at = $1, updated_at = $1 WHERE environment_id = $2 AND environment_owner = $3 "
                "AND environment_deleted_at = ''",
                (now, environment_id, owner_user),
            )

    # --- 快照 ---

    async def latest(self, owner: str, workspace_id: str) -> Snapshot | None:
        await self.migrate()
        async with self._db(owner).transaction(read_only=True) as tx:
            rows = await tx.query(
                f"SELECT {', '.join(_SNAP_COLUMNS)} FROM workspace_snapshots WHERE workspace_id = $1 AND deleted_at = '' ORDER BY seq DESC",
                (workspace_id,),
            )
        return Snapshot.from_row(rows[0]) if rows else None

    async def entries(self, owner: str, snapshot: Snapshot) -> list[FileEntry]:
        """快照的清单。"""
        try:
            data = (await self._bucket(owner).get(snapshot.manifest_key)).data
        except NotFoundError:
            raise TransientError("workspace_restore_mismatch", "快照的清单不见了（被清理了？），换最新的快照再试") from None
        try:
            doc = json.loads(data)
            return [FileEntry.from_dict(e) for e in doc["entries"]]
        except (ValueError, KeyError, TypeError):
            raise InvalidError("workspace_manifest_corrupt", "快照的清单格式不对") from None

    async def snapshot(self, box: Any, env_id: str, owner: str, workspace_id: str, *, max_bytes: int, env_ref: str = "") -> Snapshot:
        """把环境里的工作区存一份快照，返回它。"""
        _require_archive(box)
        bucket = self._bucket(owner)
        previous = await self.latest(owner, workspace_id)
        known: set[str] = set()
        if previous is not None:
            with contextlib.suppress(TransientError, InvalidError):
                known = {e.sha256 for e in await self.entries(owner, previous) if e.type == "file"}
        with self._tempfile() as tmp:
            await box.export_archive(env_id, tmp, max_bytes=max_bytes)
            tmp.seek(0)
            entries, total, new_blobs = await self._store_blobs(bucket, workspace_id, tmp, max_bytes=max_bytes, known=known)
        snapshot_id = new_snapshot_id()
        now = iso(self._clock())
        snap = Snapshot(
            snapshot_id=snapshot_id, workspace_id=workspace_id, manifest_key=manifest_key(workspace_id, snapshot_id), created_at=now,
            files=sum(1 for e in entries if e.type == "file"), size_bytes=total, new_blobs=new_blobs, env_ref=env_ref,
        )
        doc = {
            "version": MANIFEST_VERSION, "workspace_id": workspace_id, "snapshot_id": snapshot_id, "created_at": now,
            "entries": [e.as_dict() for e in sorted(entries, key=lambda e: e.path)],
        }
        await bucket.put(snap.manifest_key, json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode(), content_type="application/json")
        await self.migrate()
        async with self._db(owner).transaction() as tx:
            row = await tx.query_one("SELECT MAX(seq) AS s FROM workspace_snapshots WHERE workspace_id = $1", (workspace_id,))
            snap = replace(snap, seq=int((row or {}).get("s") or 0) + 1)
            await tx.execute(
                f"INSERT INTO workspace_snapshots ({', '.join(_SNAP_COLUMNS)}) VALUES ({', '.join(f'${i}' for i in range(1, len(_SNAP_COLUMNS) + 1))})",
                [getattr(snap, c) for c in _SNAP_COLUMNS],
            )
            await tx.execute(
                "UPDATE workspaces SET snapshot_id = $1, size_bytes = $2, files = $3, updated_at = $4 WHERE workspace_id = $5",
                (snapshot_id, total, snap.files, now, workspace_id),
            )
        await self._prune(owner, workspace_id)
        return snap

    async def _store_blobs(
        self, bucket: ObjectStore, workspace_id: str, src: IO[bytes], *, max_bytes: int, known: set[str]
    ) -> tuple[list[FileEntry], int, int]:
        entries: list[FileEntry] = []
        seen: set[str] = set()
        total = new_blobs = 0
        try:
            with tarfile.open(fileobj=src, mode="r|") as tar:
                for member in tar:
                    rel = clean_name(member.name)
                    if rel == "." or rel == ".agentruntime":
                        continue
                    if rel in seen:
                        raise InvalidError("invalid_archive", "导出的存档里有重名的项")
                    seen.add(rel)
                    mode = member.mode & 0o777
                    if member.isdir():
                        entries.append(FileEntry(path=rel, type="dir", mode=mode))
                    elif member.issym():
                        if not link_inside(rel, member.linkname):
                            continue  # 指出工作区的链接不存（与 agent 导出时一样）
                        entries.append(FileEntry(path=rel, type="symlink", mode=0, link=member.linkname))
                    elif member.isreg():
                        total += member.size
                        if total > max_bytes:
                            raise InvalidError("too_large", f"工作区超过上限 {max_bytes} 字节")
                        reader = tar.extractfile(member)
                        data = reader.read() if reader is not None else b""
                        if len(data) != member.size:
                            raise TransientError("workspace_archive_truncated", "导出的存档不完整")
                        sha = hashlib.sha256(data).hexdigest()
                        if sha not in known:
                            key = blob_key(workspace_id, sha)
                            try:
                                await bucket.head(key)
                            except NotFoundError:
                                await bucket.put(key, data)
                                new_blobs += 1
                            known.add(sha)
                        entries.append(FileEntry(path=rel, type="file", mode=mode, size=len(data), sha256=sha))
                    else:
                        raise InvalidError("invalid_archive", "导出的存档里只能有普通文件、目录与符号链接")
        except tarfile.TarError:
            raise TransientError("workspace_archive_truncated", "导出的存档不是完整的 tar") from None
        return entries, total, new_blobs

    async def restore(self, box: Any, env_id: str, owner: str, workspace_id: str, *, max_bytes: int, snapshot: Snapshot | None = None) -> Snapshot | None:
        """把最近的（或给定的）快照恢复进一个空的环境；没有快照时什么也不做，返回 None。"""
        _require_archive(box)
        snap = snapshot or await self.latest(owner, workspace_id)
        if snap is None:
            return None
        entries = await self.entries(owner, snap)
        bucket = self._bucket(owner)
        with self._tempfile() as tmp:
            with tarfile.open(fileobj=tmp, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for entry in sorted(entries, key=lambda e: e.path):
                    info = tarfile.TarInfo(entry.path)
                    info.mode = entry.mode & 0o777
                    if entry.type == "dir":
                        info.type = tarfile.DIRTYPE
                        tar.addfile(info)
                    elif entry.type == "symlink":
                        info.type, info.linkname, info.mode = tarfile.SYMTYPE, entry.link, 0o777
                        tar.addfile(info)
                    else:
                        try:
                            data = (await bucket.get(blob_key(workspace_id, entry.sha256))).data
                        except NotFoundError:
                            raise TransientError("workspace_restore_mismatch", f"快照里 {entry.path} 的内容不见了") from None
                        if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
                            raise TransientError("workspace_restore_mismatch", f"快照里 {entry.path} 的内容与清单不符")
                        info.size = len(data)
                        tar.addfile(info, io.BytesIO(data))
            size = tmp.tell()
            tmp.seek(0)
            await box.import_archive(env_id, tmp, size=size, max_bytes=max(max_bytes, snap.size_bytes, 1))
        got = await box.manifest(env_id)
        if shape(got) != shape(entries):
            raise TransientError("workspace_restore_mismatch", "恢复后环境里的文件与快照的清单不一致")
        return snap

    # --- 清理与删除 ---

    async def _prune(self, owner: str, workspace_id: str) -> None:
        """只留最近 keep 份快照；没人引用的块删掉。"""
        async with self._db(owner).transaction(read_only=True) as tx:
            rows = await tx.query(
                f"SELECT {', '.join(_SNAP_COLUMNS)} FROM workspace_snapshots WHERE workspace_id = $1 AND deleted_at = '' ORDER BY seq DESC",
                (workspace_id,),
            )
        snaps = [Snapshot.from_row(r) for r in rows]
        kept, dropped = snaps[: self.keep], snaps[self.keep :]
        bucket = self._bucket(owner)
        now = iso(self._clock())
        for snap in dropped:
            await bucket.delete(snap.manifest_key)
            async with self._db(owner).transaction() as tx:
                await tx.execute("UPDATE workspace_snapshots SET deleted_at = $1 WHERE snapshot_id = $2", (now, snap.snapshot_id))
        referenced: set[str] = set()
        for snap in kept:
            referenced |= {e.sha256 for e in await self.entries(owner, snap) if e.type == "file"}
        prefix = f"workspaces/{workspace_id}/blobs/"
        for key in await _keys(bucket, prefix):
            if key[len(prefix):] not in referenced:
                await bucket.delete(key)

    async def delete(self, owner: str, workspace_id: str) -> bool:
        """删掉工作区的存档（块、清单）与快照，行记为 deleted；返回删除前是否存在。"""
        record = await self.get(owner, workspace_id)
        bucket = self._bucket(owner)
        for key in await _keys(bucket, f"workspaces/{workspace_id}/"):
            await bucket.delete(key)
        now = iso(self._clock())
        async with self._db(owner).transaction() as tx:
            await tx.execute("UPDATE workspace_snapshots SET deleted_at = $1 WHERE workspace_id = $2 AND deleted_at = ''", (now, workspace_id))
            await tx.execute(
                "UPDATE workspaces SET state = 'deleted', snapshot_id = '', size_bytes = 0, files = 0, updated_at = $1, deleted_at = $1 "
                "WHERE workspace_id = $2",
                (now, workspace_id),
            )
        return record is not None and record.state != "deleted"

    @contextlib.contextmanager
    def _tempfile(self) -> Any:
        with tempfile.TemporaryFile(dir=self._tmp_dir) as tmp:
            yield tmp


async def _keys(bucket: ObjectStore, prefix: str) -> list[str]:
    out: list[str] = []
    cursor: str | None = None
    while True:
        page = await bucket.list(prefix=prefix, limit=1000, cursor=cursor)
        out += [item.key for item in page.items]
        if not page.next_cursor:
            return out
        cursor = page.next_cursor


def _require_archive(box: Any) -> None:
    if not all(hasattr(box, m) for m in ("export_archive", "import_archive", "manifest")):
        raise InvalidError("unsupported_archive", "这个沙箱后端不能导出、导入工作区")


__all__ = ["KEEP", "MIGRATIONS", "Snapshot", "WorkspaceArchiver", "WorkspaceRecord", "blob_key", "manifest_key", "shape"]
