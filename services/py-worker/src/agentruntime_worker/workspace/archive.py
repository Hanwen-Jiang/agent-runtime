"""工作区的记录与存档（设计书改动稿 W2；D-06c，docs/plan/workspace-layer.md 第 2、3 节）。

把工作区搬进搬出环境（存档：从环境导出 tar、按 sha256 存块、写清单；恢复：拼 tar、导入、逐项核对）在 Sandbox Service 里
（SBX-01，services/go-sandbox-service 的 internal/archive，存储格式不变）。Worker 这边只留读写记录与 ObjectStore 里的元数据：

- 存储格式：块在 ObjectStore 租户作用域的 workspaces/<ws>/blobs/<sha256>，清单在 workspaces/<ws>/snapshots/<id>.json，
  快照表记一行；只留最近 keep 份，没人引用的块删掉。
- Worker 用到的：工作区的行（建、改状态、版本号、选用的环境配置）、最近的快照、复制快照（N-10，只记引用、不搬块）、删除。
- 业务库（迁移 205，租户作用域）：workspaces、workspace_snapshots、workspace_pushes（推送的 gate 与幂等记录，见 operations）。

同一个工作区同一时间只有一个可写的环境（沙箱环境表的规则），所以同一个工作区的存档与清理不会并发。
"""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from agentruntime_core.dev._common import PLATFORM_ID
from agentruntime_core.errors import (
    ConflictError,
    DeniedError,
    InvalidError,
    NotFoundError,
    TransientError,
)
from agentruntime_core.providers.object_store import ObjectStore, ObjectStoreProvider
from agentruntime_core.providers.relational_store import (
    Migration,
    RelationalStore,
    RelationalStoreProvider,
)
from agentruntime_core.providers.sandbox import FileEntry
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.env_config import BoundEnvironment, EnvironmentConfig
from agentruntime_worker.sandbox.policy import WorkspaceLimits
from agentruntime_worker.sandbox.times import iso, utc_now
from agentruntime_worker.workspace import timing

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
        # 机密的部分（.env 与网络密钥的引用）另放一列：删除环境配置时只清这一列（2026-11-03 用户定）。
        "ALTER TABLE workspaces ADD COLUMN environment_secrets TEXT NOT NULL DEFAULT ''",
        "CREATE INDEX workspaces_environment ON workspaces (environment_id, environment_owner)",
        # N-10：复制工作区时新快照引用源工作区的块（不复制）；记下引用了哪些工作区的块（,ws1,ws2,），清理时据此保留它们。
        "ALTER TABLE workspace_snapshots ADD COLUMN blob_sources TEXT NOT NULL DEFAULT ''",
    )),
    # WS-READ-03：工作区的版本号。改动工作区的（Workspace API 的写操作、写类工具）都加一；task-service 按它缓存只读操作的结果。
    Migration(210, "workspace_generation", (
        "ALTER TABLE workspaces ADD COLUMN generation BIGINT NOT NULL DEFAULT 0",
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
    # 清单里有块放在别的工作区之下（N-10，复制来的）：那些工作区，写成 ,ws1,ws2,；空表示块都在本工作区之下。
    blob_sources: str = ""

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

    async def bump_generation(self, owner: str, workspace_id: str) -> None:
        """工作区改动过：版本号加一（WS-READ-03）。没有这一行时什么都不做（不凭空建行）。"""
        await self.migrate()
        async with self._db(owner).transaction() as tx:
            await tx.execute("UPDATE workspaces SET generation = generation + 1 WHERE workspace_id = $1", (workspace_id,))

    async def generation(self, owner: str, workspace_id: str) -> int | None:
        """工作区的版本号；没有这一行时 None。"""
        await self.migrate()
        async with self._db(owner).transaction() as tx:
            row = await tx.query_one("SELECT generation FROM workspaces WHERE workspace_id = $1", (workspace_id,))
        return int(row["generation"]) if row else None

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
                    "environment_deleted_at = '', environment_secrets = '' WHERE workspace_id = $3",
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

    async def transition(self, owner: str, workspace_id: str, from_state: str, to_state: str) -> bool:
        """冷热分层（DISK-B）的状态变化：只在当前是 from_state 时改成 to_state，返回改没改成（谁先改成谁做，别人让开）。
        active → cooling → cold → restoring → active；中途失败退回前一个状态。不动 updated_at（它算闲置）。"""
        await self.migrate()
        async with self._db(owner).transaction() as tx:
            changed = await tx.execute(
                "UPDATE workspaces SET state = $1 WHERE workspace_id = $2 AND state = $3", (to_state, workspace_id, from_state)
            )
        return changed == 1

    # --- 选用的环境配置（ENV-02）---

    async def set_environment(self, owner: str, workspace_id: str, config: EnvironmentConfig, *, owner_user: str) -> None:
        """记下工作区选用的环境配置（建会话时的快照）；之后编辑环境只影响新的会话。非机密的部分与机密的部分分两列存。
        同一个配置在这个工作区上已经删除时只收非机密的快照（会话保留设置，不再拿机密），带了机密就 environment_deleted。"""
        await self.migrate()
        now = iso(self._clock())
        async with self._db(owner).transaction() as tx:
            row = await tx.query_one("SELECT environment_id, environment_deleted_at FROM workspaces WHERE workspace_id = $1", (workspace_id,))
            deleted = bool(row and row["environment_id"] == config.environment_id and row["environment_deleted_at"])
            if deleted and config.has_secrets:
                raise DeniedError("environment_deleted", "这个环境配置已经删除：会话只保留非机密的设置，不再接收 .env 与网络密钥")
            await tx.execute(
                "UPDATE workspaces SET environment_id = $1, environment = $2, environment_secrets = $3, environment_owner = $4, "
                "environment_deleted_at = $5, updated_at = $6 WHERE workspace_id = $7",
                (config.environment_id, config.public_json(), config.secrets_json(), owner_user, str(row["environment_deleted_at"]) if deleted else "",
                 now, workspace_id),
            )

    async def clear_environment(self, owner: str, workspace_id: str) -> None:
        await self.migrate()
        async with self._db(owner).transaction() as tx:
            await tx.execute(
                "UPDATE workspaces SET environment_id = '', environment = '', environment_secrets = '', environment_owner = '', "
                "environment_deleted_at = '', updated_at = $1 WHERE workspace_id = $2",
                (iso(self._clock()), workspace_id),
            )

    async def environment(self, owner: str, workspace_id: str) -> BoundEnvironment | None:
        """工作区选用的环境配置；没有选用（或工作区不在）时 None。配置删除之后只剩非机密的部分。"""
        if owner == PLATFORM_ID or not owner:
            return None
        await self.migrate()
        async with self._db(owner).transaction(read_only=True) as tx:
            row = await tx.query_one(
                "SELECT environment, environment_secrets, environment_owner, environment_deleted_at FROM workspaces "
                "WHERE workspace_id = $1 AND state <> 'deleted'",
                (workspace_id,),
            )
        if not row or not row["environment"]:
            return None
        config = EnvironmentConfig.from_parts(str(row["environment"]), str(row["environment_secrets"] or ""))
        return BoundEnvironment(config, str(row["environment_owner"]), bool(row["environment_deleted_at"]))

    async def delete_environment(self, owner: str, environment_id: str, *, owner_user: str) -> int:
        """删除环境配置（Q35；task-service 的 DeleteEnvironmentConfig 做同样的事）：立即删掉机密的部分、记下删除时间，
        会话照常用非机密的快照；返回新标记的工作区个数。"""
        await self.migrate()
        now = iso(self._clock())
        async with self._db(owner).transaction() as tx:
            return await tx.execute(
                "UPDATE workspaces SET environment_secrets = '', environment_deleted_at = $1, updated_at = $1 WHERE environment_id = $2 "
                "AND environment_owner = $3 AND environment_deleted_at = ''",
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
        return (await self._manifest(owner, snapshot))[0]

    async def _manifest(self, owner: str, snapshot: Snapshot) -> tuple[list[FileEntry], dict[str, str]]:
        """快照的清单与“块在别的工作区之下”的那一部分（sha256 → 工作区，N-10）。"""
        try:
            data = (await self._bucket(owner).get(snapshot.manifest_key)).data
        except NotFoundError:
            raise TransientError("workspace_restore_mismatch", "快照的清单不见了（被清理了？），换最新的快照再试") from None
        timing.add_bytes("object_store", "in", len(data))
        try:
            doc = json.loads(data)
            foreign = {str(k): str(v) for k, v in dict(doc.get("blob_workspaces") or {}).items()}
            return [FileEntry.from_dict(e) for e in doc["entries"]], foreign
        except (ValueError, KeyError, TypeError, AttributeError):
            raise InvalidError("workspace_manifest_corrupt", "快照的清单格式不对") from None

    # --- 清理与删除 ---

    async def _prune(self, owner: str, workspace_id: str) -> None:
        """只留最近 keep 份快照；没人引用的块删掉（别的工作区复制来的快照引用的块也算有人引用，N-10）。"""
        async with self._db(owner).transaction(read_only=True) as tx:
            rows = await tx.query(
                f"SELECT {', '.join(_SNAP_COLUMNS)} FROM workspace_snapshots WHERE workspace_id = $1 AND deleted_at = '' ORDER BY seq DESC",
                (workspace_id,),
            )
        snaps = [Snapshot.from_row(r) for r in rows]
        kept, dropped = snaps[: self.keep], snaps[self.keep :]
        bucket = self._bucket(owner)
        now = iso(self._clock())
        sources: set[str] = set()
        for snap in dropped:
            sources |= _source_set(snap.blob_sources)
            await bucket.delete(snap.manifest_key)
            async with self._db(owner).transaction() as tx:
                await tx.execute("UPDATE workspace_snapshots SET deleted_at = $1 WHERE snapshot_id = $2", (now, snap.snapshot_id))
        referenced: set[str] = set()
        for snap in kept:
            entries, foreign = await self._manifest(owner, snap)
            referenced |= {e.sha256 for e in entries if e.type == "file" and e.sha256 not in foreign}
        await self._delete_blobs(owner, workspace_id, keep=referenced | await self._shared(owner, workspace_id))
        await self._collect_deleted_sources(owner, sources)

    async def _shared(self, owner: str, workspace_id: str) -> set[str]:
        """别的工作区的快照引用的、放在 workspace_id 之下的块（复制来的，N-10）。"""
        async with self._db(owner).transaction(read_only=True) as tx:
            rows = await tx.query(
                f"SELECT {', '.join(_SNAP_COLUMNS)} FROM workspace_snapshots WHERE deleted_at = '' AND workspace_id <> $1 AND blob_sources LIKE $2",
                (workspace_id, f"%,{workspace_id},%"),
            )
        out: set[str] = set()
        for row in rows:
            with contextlib.suppress(TransientError):
                _, foreign = await self._manifest(owner, Snapshot.from_row(row))
                out |= {sha for sha, ws in foreign.items() if ws == workspace_id}
        return out

    async def _delete_blobs(self, owner: str, workspace_id: str, *, keep: set[str]) -> None:
        bucket = self._bucket(owner)
        prefix = f"workspaces/{workspace_id}/blobs/"
        for key in await _keys(bucket, prefix):
            if key[len(prefix):] not in keep:
                await bucket.delete(key)

    async def _collect_deleted_sources(self, owner: str, sources: set[str]) -> None:
        """源工作区已经删除、只为复制来的快照留着的块：没人引用了就删掉。"""
        for source in sorted(sources):
            record = await self.get(owner, source)
            if record is None or record.state == "deleted":
                await self._delete_blobs(owner, source, keep=await self._shared(owner, source))

    async def copy_snapshot(self, owner: str, source: str, workspace_id: str, *, max_bytes: int) -> tuple[Snapshot, str, str] | None:
        """复制工作区（N-10）：新工作区的快照引用源工作区最近一份快照的清单与块，不搬内容、不起环境；之后改了的文件才写进
        新工作区自己的块。源工作区还没有快照时 None（调用方照旧在环境里复制）。返回（快照，HEAD，分支）。"""
        src = await self.latest(owner, source)
        if src is None:
            return None
        if src.size_bytes > max_bytes:
            raise InvalidError("too_large", f"工作区超过上限 {max_bytes} 字节")
        entries, foreign = await self._manifest(owner, src)
        located = {e.sha256: foreign.get(e.sha256, source) for e in entries if e.type == "file"}
        snapshot_id = new_snapshot_id()
        now = iso(self._clock())
        snap = Snapshot(
            snapshot_id=snapshot_id, workspace_id=workspace_id, manifest_key=manifest_key(workspace_id, snapshot_id), created_at=now, seq=1,
            files=src.files, size_bytes=src.size_bytes, new_blobs=0, blob_sources=_sources(located),
        )
        doc = {
            "version": MANIFEST_VERSION, "workspace_id": workspace_id, "snapshot_id": snapshot_id, "created_at": now,
            "entries": [e.as_dict() for e in sorted(entries, key=lambda e: e.path)], "blob_workspaces": dict(sorted(located.items())),
        }
        bucket = self._bucket(owner)
        await bucket.put(snap.manifest_key, json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode(), content_type="application/json")
        async with self._db(owner).transaction() as tx:
            await tx.execute(
                f"INSERT INTO workspace_snapshots ({', '.join(_SNAP_COLUMNS)}) VALUES ({', '.join(f'${i}' for i in range(1, len(_SNAP_COLUMNS) + 1))})",
                [getattr(snap, c) for c in _SNAP_COLUMNS],
            )
            await tx.execute(
                "UPDATE workspaces SET snapshot_id = $1, size_bytes = $2, files = $3, updated_at = $4 WHERE workspace_id = $5",
                (snapshot_id, snap.size_bytes, snap.files, now, workspace_id),
            )
        by_path = {e.path: e for e in entries}

        async def read(path: str) -> bytes | None:
            entry = by_path.get(path)
            if entry is None or entry.type != "file":
                return None
            return (await bucket.get(blob_key(located[entry.sha256], entry.sha256))).data

        head, branch = await git_head(read)
        return snap, head, branch

    async def delete(self, owner: str, workspace_id: str) -> bool:
        """删掉工作区的存档（块、清单）与快照，行记为 deleted；返回删除前是否存在。别的工作区复制来的快照还引用的块先留着
        （N-10），等它们不引用了再删。"""
        record = await self.get(owner, workspace_id)
        bucket = self._bucket(owner)
        shared = await self._shared(owner, workspace_id)
        sources: set[str] = set()
        async with self._db(owner).transaction(read_only=True) as tx:
            for row in await tx.query("SELECT blob_sources FROM workspace_snapshots WHERE workspace_id = $1 AND deleted_at = ''", (workspace_id,)):
                sources |= _source_set(str(row["blob_sources"] or ""))
        blobs = f"workspaces/{workspace_id}/blobs/"
        for key in await _keys(bucket, f"workspaces/{workspace_id}/"):
            if key.startswith(blobs) and key[len(blobs):] in shared:
                continue
            await bucket.delete(key)
        now = iso(self._clock())
        async with self._db(owner).transaction() as tx:
            await tx.execute("UPDATE workspace_snapshots SET deleted_at = $1 WHERE workspace_id = $2 AND deleted_at = ''", (now, workspace_id))
            await tx.execute(
                "UPDATE workspaces SET state = 'deleted', snapshot_id = '', size_bytes = 0, files = 0, updated_at = $1, deleted_at = $1 "
                "WHERE workspace_id = $2",
                (now, workspace_id),
            )
        await self._collect_deleted_sources(owner, sources)
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


def _sources(located: Mapping[str, str]) -> str:
    names = sorted(set(located.values()))
    return f",{','.join(names)}," if names else ""


def _source_set(text: str) -> set[str]:
    return {n for n in text.split(",") if n}


async def git_head(read: Callable[[str], Awaitable[bytes | None]]) -> tuple[str, str]:
    """不跑 git，按 .git/HEAD、松散的 ref 与 packed-refs 读出 HEAD 的提交与分支（N-10 的复制要它）；读不出时为空。"""
    head = ((await read(".git/HEAD")) or b"").decode("utf-8", errors="replace").strip()
    if not head.startswith("ref: "):
        return (head if _HEX.match(head) else ""), ""
    ref = head[5:].strip()
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ""
    if ".." in ref.split("/") or not ref.startswith("refs/"):
        return "", branch
    loose = ((await read(f".git/{ref}")) or b"").decode("utf-8", errors="replace").strip()
    if _HEX.match(loose):
        return loose, branch
    for line in ((await read(".git/packed-refs")) or b"").decode("utf-8", errors="replace").splitlines():
        sha, _, name = line.partition(" ")
        if name.strip() == ref and _HEX.match(sha):
            return sha, branch
    return "", branch


_HEX = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


__all__ = ["KEEP", "MIGRATIONS", "Snapshot", "WorkspaceArchiver", "WorkspaceRecord", "blob_key", "manifest_key", "shape"]
