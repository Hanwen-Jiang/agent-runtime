"""沙箱环境表（设计书改动稿 W9；docs/plan/sandbox-platform.md 第 2 节；D-11）。

Runtime 自己记下每个环境，所有后端的建与删都经过这里；对账（sandbox.reconcile）按它清孤儿、记丢失、回收到期的环境；
用量与花费也从这里出。改动稿草稿里的叫法与调研文档里 Task State 的旧叫法撞了（术语检查拦它），所以这里叫环境表。

- 表在业务库（RelationalStore，按租户行级安全；开发时是 SQLite，与 task-service、包镜像、出网网关同一套布局），迁移版本 201 起。
- 状态：creating → ready → destroying → destroyed；没建成是 failed，后端里没了是 lost。调后端之前先落账（creating、
  destroying），之后补状态；任何一步中断留下的状态由对账修，不靠进程内的记忆。
- 建环境时在同一个事务里核对配额与预算（W10）：先更新本租户 sandbox_quota_lock 的那一行（Postgres 上是行锁），并发建
  环境在这一步串行，不会多出来。超了 denied（sandbox_quota_exceeded、sandbox_budget_exceeded），不排队、不降级。
- 平台作用域的 sandbox_env_directory 记着每个环境属于哪个作用域，只给对账用（先写目录再写租户的行）。
- 时间一律是 ISO 8601 UTC 文本（可以按字典序比较）；布尔是 0/1。
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from agentruntime_core.dev._common import PLATFORM_ID
from agentruntime_core.errors import DeniedError, InvalidError
from agentruntime_core.providers.relational_store import Migration, RelationalStore, RelationalStoreProvider, Transaction
from agentruntime_core.tenancy import TenantContext

PURPOSE = "sandbox"
ACTIVE = ("creating", "ready", "destroying")
TERMINAL = ("destroyed", "failed", "lost")

MIGRATIONS = (
    Migration(201, "sandbox_envs", (
        """CREATE TABLE sandbox_envs (
            env_ref TEXT PRIMARY KEY,
            backend TEXT NOT NULL,
            kind TEXT NOT NULL,
            isolation TEXT NOT NULL,
            backend_env_id TEXT NOT NULL DEFAULT '',
            workspace_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            writable BIGINT NOT NULL DEFAULT 0,
            runtime TEXT NOT NULL DEFAULT '',
            image TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            ready_at TEXT NOT NULL DEFAULT '',
            last_used_at TEXT NOT NULL DEFAULT '',
            expires_at TEXT NOT NULL DEFAULT '',
            max_until TEXT NOT NULL DEFAULT '',
            destroyed_at TEXT NOT NULL DEFAULT '',
            idle_seconds BIGINT NOT NULL DEFAULT 0,
            busy_until TEXT NOT NULL DEFAULT '',
            cpu_ms BIGINT NOT NULL DEFAULT 0,
            alive_ms BIGINT NOT NULL DEFAULT 0,
            cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
            fallback_from TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX sandbox_envs_session ON sandbox_envs (workspace_id, session_id, state)",
        "CREATE INDEX sandbox_envs_backend ON sandbox_envs (backend, backend_env_id)",
        "CREATE INDEX sandbox_envs_state ON sandbox_envs (state)",
        """CREATE TABLE sandbox_usage (
            day TEXT PRIMARY KEY,
            sandbox_seconds BIGINT NOT NULL DEFAULT 0,
            egress_bytes BIGINT NOT NULL DEFAULT 0,
            mirror_miss_bytes BIGINT NOT NULL DEFAULT 0,
            cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0
        )""",
        "CREATE TABLE sandbox_quota_lock (id TEXT PRIMARY KEY, version BIGINT NOT NULL DEFAULT 0)",
        """CREATE TABLE sandbox_env_directory (
            env_ref TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            backend TEXT NOT NULL,
            state TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX sandbox_env_directory_state ON sandbox_env_directory (state)",
    )),
    Migration(202, "sandbox_grants", (
        """CREATE TABLE sandbox_grants (
            grant_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            hosts TEXT NOT NULL,
            ports TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            max_up_bytes BIGINT NOT NULL DEFAULT 0,
            max_down_bytes BIGINT NOT NULL DEFAULT 0,
            max_conns BIGINT NOT NULL DEFAULT 0,
            max_rps BIGINT NOT NULL DEFAULT 0,
            up_bytes BIGINT NOT NULL DEFAULT 0,
            down_bytes BIGINT NOT NULL DEFAULT 0,
            preview_port BIGINT NOT NULL DEFAULT 0,
            state TEXT NOT NULL,
            approved_by TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            revoked_at TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX sandbox_grants_session ON sandbox_grants (session_id, state)",
        "CREATE INDEX sandbox_grants_task ON sandbox_grants (task_id, state)",
    )),
    # D-16：预览地址落在哪个环境、转发到哪里；托管后端按域名放行时环境建起来带的主机名单；出网网关逐连接的审计。
    Migration(203, "sandbox_egress", (
        "ALTER TABLE sandbox_grants ADD COLUMN preview_env TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sandbox_grants ADD COLUMN preview_target TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sandbox_envs ADD COLUMN allow_hosts TEXT NOT NULL DEFAULT ''",
        """CREATE TABLE sandbox_egress_audit (
            audit_id TEXT PRIMARY KEY,
            grant_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            env_ref TEXT NOT NULL,
            direction TEXT NOT NULL,
            host TEXT NOT NULL,
            port BIGINT NOT NULL DEFAULT 0,
            address TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            duration_ms BIGINT NOT NULL DEFAULT 0,
            up_bytes BIGINT NOT NULL DEFAULT 0,
            down_bytes BIGINT NOT NULL DEFAULT 0,
            result TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX sandbox_egress_audit_task ON sandbox_egress_audit (task_id, started_at)",
    )),
    # D-17：按项目定制的运行环境（构建出的镜像按“定义的哈希 + 基础镜像 digest”缓存，见 sandbox.environments）。
    Migration(204, "sandbox_images", (
        """CREATE TABLE sandbox_images (
            image_key TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            path TEXT NOT NULL,
            workspace_id TEXT NOT NULL DEFAULT '',
            context TEXT NOT NULL DEFAULT '',
            dockerfile TEXT NOT NULL,
            base_image TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            image TEXT NOT NULL DEFAULT '',
            digest TEXT NOT NULL DEFAULT '',
            size_bytes BIGINT NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            ready_at TEXT NOT NULL DEFAULT '',
            last_used_at TEXT NOT NULL DEFAULT '',
            build_ms BIGINT NOT NULL DEFAULT 0,
            deleted_at TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX sandbox_images_state ON sandbox_images (state, last_used_at)",
    )),
)


def iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_env_ref() -> str:
    return "sbe-" + secrets.token_hex(8)


@dataclass(frozen=True)
class Quota:
    """租户策略的 sandbox.quota（W10）；0 表示不限。"""

    max_concurrent: int = 0
    daily_sandbox_seconds: int = 0
    daily_egress_bytes: int = 0
    daily_mirror_miss_bytes: int = 0
    daily_cost_usd: float = 0.0
    monthly_cost_usd: float = 0.0

    @classmethod
    def from_policy(cls, doc: Mapping[str, Any] | None) -> Quota:
        doc = doc or {}
        values: dict[str, Any] = {}
        for f in fields(cls):
            if f.name in doc:
                values[f.name] = float(doc[f.name]) if f.name.endswith("_usd") else int(doc[f.name])
        return cls(**values)


@dataclass(frozen=True)
class EnvRecord:
    env_ref: str
    owner: str
    backend: str
    kind: str
    isolation: str
    state: str
    created_at: str
    backend_env_id: str = ""
    workspace_id: str = ""
    session_id: str = ""
    task_id: str = ""
    writable: int = 0
    runtime: str = ""
    image: str = ""
    ready_at: str = ""
    last_used_at: str = ""
    expires_at: str = ""
    max_until: str = ""
    destroyed_at: str = ""
    idle_seconds: int = 0
    busy_until: str = ""
    cpu_ms: int = 0
    alive_ms: int = 0
    cost_usd: float = 0.0
    fallback_from: str = ""
    reason: str = ""
    # 托管后端（没有出网网关）按域名放行时，环境建起来带的主机名单（JSON 数组，排好序）；空表示断网（D-16）。
    allow_hosts: str = ""

    @classmethod
    def from_row(cls, owner: str, row: Mapping[str, Any]) -> EnvRecord:
        names = {f.name for f in fields(cls)} - {"owner"}
        return cls(owner=owner, **{k: row[k] for k in names if k in row})

    def expired(self, now: datetime) -> bool:
        return bool(self.expires_at and self.expires_at <= iso(now)) or bool(self.max_until and self.max_until <= iso(now))


@dataclass(frozen=True)
class DirectoryEntry:
    env_ref: str
    owner: str
    backend: str
    state: str
    updated_at: str


@dataclass(frozen=True)
class Usage:
    day: str
    sandbox_seconds: int = 0
    egress_bytes: int = 0
    mirror_miss_bytes: int = 0
    cost_usd: float = 0.0


_COLUMNS = [f.name for f in fields(EnvRecord) if f.name != "owner"]


class SandboxLedger:
    def __init__(self, store: RelationalStoreProvider, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._store = store
        self._clock = clock
        self._migrated = False

    @property
    def clock(self) -> Callable[[], datetime]:
        return self._clock

    async def migrate(self) -> None:
        if not self._migrated:
            await self._store.migrate(MIGRATIONS)
            self._migrated = True

    def bind(self, owner: str) -> RelationalStore:
        """owner 是租户 ID 或 _platform；租户条件由存储负责，SQL 里不写。"""
        if owner == PLATFORM_ID:
            return self._store.bind_platform(PURPOSE)
        return self._store.bind(TenantContext(tenant_id=owner))

    # --- 建 ---

    async def begin_create(
        self,
        owner: str,
        *,
        backend: str,
        kind: str,
        isolation: str,
        workspace_id: str = "",
        session_id: str = "",
        task_id: str = "",
        writable: bool = True,
        runtime: str = "",
        image: str = "",
        idle: timedelta,
        max_lifetime: timedelta,
        quota: Quota | None = None,
        fallback_from: str = "",
        allow_hosts: str = "",
        exclusive: bool = False,
    ) -> EnvRecord:
        """落一行 creating（先写平台目录再写租户的行），同一个事务里核对配额与预算。超了 denied。
        exclusive（存档模式的工作区，D-06）：这个工作区不管哪个会话（含没有会话的一次性环境）有可写的环境都算占用。"""
        await self.migrate()
        now = self._clock()
        record = EnvRecord(
            env_ref=new_env_ref(), owner=owner, backend=backend, kind=kind, isolation=isolation, state="creating", created_at=iso(now),
            workspace_id=workspace_id, session_id=session_id, task_id=task_id, writable=1 if writable else 0, runtime=runtime, image=image,
            idle_seconds=int(idle.total_seconds()), max_until=iso(now + max_lifetime), fallback_from=fallback_from, allow_hosts=allow_hosts,
        )
        await self._directory(record, "creating")
        try:
            await self._insert_checked(record, quota, now, exclusive=exclusive)
        except BaseException:
            await self._directory(record, "failed")
            raise
        return record

    async def _insert_checked(self, record: EnvRecord, quota: Quota | None, now: datetime, *, exclusive: bool = False) -> None:
        workspace_id, session_id, writable = record.workspace_id, record.session_id, bool(record.writable)
        async with self.bind(record.owner).transaction() as tx:
            await tx.execute(
                "INSERT INTO sandbox_quota_lock (id, version) VALUES ('envs', 1) ON CONFLICT (id) DO UPDATE SET version = sandbox_quota_lock.version + 1"
            )
            if quota is not None:
                await self._check_quota(tx, quota, now)
            if writable and workspace_id:
                # 一个工作区同一时间只有一个可写的环境（W5 第 3 条）。挂卷时同一个会话的几个环境看到的是同一处，只拦别的会话；
                # 存档模式（exclusive）每个环境各有一份，谁都拦。
                same_session = "" if exclusive else "AND session_id <> $2 "
                row = await tx.query_one(
                    f"SELECT COUNT(*) AS n FROM sandbox_envs WHERE workspace_id = $1 {same_session}AND writable = 1 "
                    "AND state IN ('creating', 'ready')",
                    (workspace_id,) if exclusive else (workspace_id, session_id),
                )
                if row and int(row["n"]) > 0:
                    # 由 broker 决定：别的会话的那个环境闲着就回收它再建（sandbox.broker），正在跑命令就报给调用方。
                    raise DeniedError("workspace_busy", "这个工作区正被另一个会话的可写环境占用，等它结束或回收")
            await tx.execute(
                f"INSERT INTO sandbox_envs ({', '.join(_COLUMNS)}) VALUES ({', '.join(f'${i}' for i in range(1, len(_COLUMNS) + 1))})",
                [getattr(record, c) for c in _COLUMNS],
            )

    async def _check_quota(self, tx: Transaction, quota: Quota, now: datetime) -> None:
        active = await tx.query("SELECT created_at, state FROM sandbox_envs WHERE state IN ('creating', 'ready', 'destroying')")
        if quota.max_concurrent and len(active) >= quota.max_concurrent:
            raise DeniedError("sandbox_quota_exceeded", f"同时最多 {quota.max_concurrent} 个沙箱环境，先等前面的结束")
        today = now.strftime("%Y-%m-%d")
        usage = await tx.query_one("SELECT sandbox_seconds, egress_bytes, mirror_miss_bytes, cost_usd FROM sandbox_usage WHERE day = $1", (today,))
        used_seconds = int((usage or {}).get("sandbox_seconds") or 0)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for row in active:
            started = parse_iso(str(row["created_at"])) or now
            used_seconds += int((now - max(started, midnight)).total_seconds())
        if quota.daily_sandbox_seconds and used_seconds >= quota.daily_sandbox_seconds:
            raise DeniedError("sandbox_quota_exceeded", f"今天的沙箱时长到了上限（{quota.daily_sandbox_seconds} 秒）")
        if quota.daily_egress_bytes and int((usage or {}).get("egress_bytes") or 0) >= quota.daily_egress_bytes:
            raise DeniedError("sandbox_quota_exceeded", "今天的出网流量到了上限")
        if quota.daily_mirror_miss_bytes and int((usage or {}).get("mirror_miss_bytes") or 0) >= quota.daily_mirror_miss_bytes:
            raise DeniedError("sandbox_quota_exceeded", "今天从上游拉依赖的流量到了上限")
        if quota.daily_cost_usd and float((usage or {}).get("cost_usd") or 0) >= quota.daily_cost_usd:
            raise DeniedError("sandbox_budget_exceeded", f"今天的沙箱花费到了上限（{quota.daily_cost_usd} 美元）")
        if quota.monthly_cost_usd:
            month = await tx.query_one("SELECT SUM(cost_usd) AS c FROM sandbox_usage WHERE day >= $1 AND day <= $2", (today[:8] + "01", today))
            if float((month or {}).get("c") or 0) >= quota.monthly_cost_usd:
                raise DeniedError("sandbox_budget_exceeded", f"本月的沙箱花费到了上限（{quota.monthly_cost_usd} 美元）")

    async def mark_ready(self, record: EnvRecord, backend_env_id: str, *, image: str = "") -> EnvRecord:
        """就绪；image 是后端报的实际镜像（按 digest，D-14），报了就以它为准。"""
        now = self._clock()
        expires = min(now + timedelta(seconds=record.idle_seconds), parse_iso(record.max_until) or now)
        ready = replace(
            record, state="ready", backend_env_id=backend_env_id, ready_at=iso(now), last_used_at=iso(now), expires_at=iso(expires),
            image=image or record.image,
        )
        await self._update(ready, ("state", "backend_env_id", "ready_at", "last_used_at", "expires_at", "image"))
        await self._directory(ready, "ready")
        return ready

    async def mark_failed(self, record: EnvRecord, reason: str) -> EnvRecord:
        failed = replace(record, state="failed", reason=reason[:300], destroyed_at=iso(self._clock()))
        await self._update(failed, ("state", "reason", "destroyed_at"))
        await self._directory(failed, "failed")
        return failed

    # --- 用 ---

    async def find_reusable(self, owner: str, *, workspace_id: str, session_id: str, backends: Sequence[str], writable: bool) -> EnvRecord | None:
        """同一个（工作区，会话）里还没到期的 ready 环境（W5）；按最近使用排，先给最近用过的。"""
        await self.migrate()
        now = iso(self._clock())
        async with self.bind(owner).transaction(read_only=True) as tx:
            rows = await tx.query(
                f"SELECT {', '.join(_COLUMNS)} FROM sandbox_envs WHERE workspace_id = $1 AND session_id = $2 AND state = 'ready' "
                "AND writable = $3 AND expires_at > $4 AND max_until > $4 ORDER BY last_used_at DESC",
                (workspace_id, session_id, 1 if writable else 0, now),
            )
        for row in rows:
            if row["backend"] in backends:
                return EnvRecord.from_row(owner, row)
        return None

    async def touch(self, record: EnvRecord, *, cpu_ms: int = 0, task_id: str = "", busy_for: timedelta | None = None) -> EnvRecord:
        """用了一次：顺延空闲到期（不超过活满），累加 CPU 时间。busy_for 给了时记下命令最晚跑到什么时候（这期间别的会话不能回收它），
        没给时清掉（命令跑完了）。"""
        now = self._clock()
        expires = min(now + timedelta(seconds=record.idle_seconds), parse_iso(record.max_until) or now)
        busy = iso(now + busy_for) if busy_for is not None else ""
        async with self.bind(record.owner).transaction() as tx:
            await tx.execute(
                "UPDATE sandbox_envs SET last_used_at = $1, expires_at = $2, cpu_ms = cpu_ms + $3, task_id = $4, busy_until = $5 "
                "WHERE env_ref = $6 AND state = 'ready'",
                (iso(now), iso(expires), int(cpu_ms), task_id or record.task_id, busy, record.env_ref),
            )
        return replace(
            record, last_used_at=iso(now), expires_at=iso(expires), cpu_ms=record.cpu_ms + int(cpu_ms), task_id=task_id or record.task_id, busy_until=busy
        )

    async def writable_holders(self, owner: str, *, workspace_id: str, session_id: str | None) -> list[EnvRecord]:
        """别的会话里占着这个工作区的可写环境（creating 或 ready）；session_id 为 None 时不分会话（存档模式，D-06）。"""
        await self.migrate()
        async with self.bind(owner).transaction(read_only=True) as tx:
            if session_id is None:
                rows = await tx.query(
                    f"SELECT {', '.join(_COLUMNS)} FROM sandbox_envs WHERE workspace_id = $1 AND writable = 1 AND state IN ('creating', 'ready')",
                    (workspace_id,),
                )
            else:
                rows = await tx.query(
                    f"SELECT {', '.join(_COLUMNS)} FROM sandbox_envs WHERE workspace_id = $1 AND session_id <> $2 AND writable = 1 "
                    "AND state IN ('creating', 'ready')",
                    (workspace_id, session_id),
                )
        return [EnvRecord.from_row(owner, r) for r in rows]

    def idle_now(self, record: EnvRecord) -> bool:
        """没有命令在跑（busy_until 空或已过），可以回收。"""
        return record.state == "ready" and (not record.busy_until or record.busy_until <= iso(self._clock()))

    # --- 删 ---

    async def begin_destroy(self, record: EnvRecord, reason: str = "") -> EnvRecord:
        destroying = replace(record, state="destroying", reason=reason[:300] or record.reason)
        await self._update(destroying, ("state", "reason"))
        await self._directory(destroying, "destroying")
        return destroying

    async def mark_destroyed(self, record: EnvRecord, *, price_per_hour: float = 0.0, price_per_cpu_hour: float = 0.0) -> EnvRecord:
        """销毁完成：存活时间从建起算，花费按后端的价目（部署登记）；计入当天的用量。"""
        now = self._clock()
        started = parse_iso(record.created_at) or now
        alive_ms = max(0, int((now - started).total_seconds() * 1000))
        cost = alive_ms / 3_600_000 * price_per_hour + record.cpu_ms / 3_600_000 * price_per_cpu_hour
        done = replace(record, state="destroyed", destroyed_at=iso(now), alive_ms=alive_ms, cost_usd=round(cost, 6))
        await self._update(done, ("state", "destroyed_at", "alive_ms", "cost_usd"))
        await self.add_usage(record.owner, now, sandbox_seconds=alive_ms // 1000, cost_usd=done.cost_usd)
        await self._directory(done, "destroyed")
        return done

    async def mark_lost(self, record: EnvRecord, reason: str) -> EnvRecord:
        lost = replace(record, state="lost", reason=reason[:300], destroyed_at=iso(self._clock()))
        await self._update(lost, ("state", "reason", "destroyed_at"))
        await self._directory(lost, "lost")
        return lost

    # --- 查 ---

    async def get(self, owner: str, env_ref: str) -> EnvRecord | None:
        await self.migrate()
        async with self.bind(owner).transaction(read_only=True) as tx:
            row = await tx.query_one(f"SELECT {', '.join(_COLUMNS)} FROM sandbox_envs WHERE env_ref = $1", (env_ref,))
        return EnvRecord.from_row(owner, row) if row else None

    async def by_backend_env(self, owner: str, backend: str | None, backend_env_id: str) -> EnvRecord | None:
        """按后端给的环境 id 找；backend 为 None 时不限后端（同一个命名空间里的 gvisor 与 kata 会互相列出对方的 Pod）。"""
        await self.migrate()
        async with self.bind(owner).transaction(read_only=True) as tx:
            if backend is None:
                rows = await tx.query(
                    f"SELECT {', '.join(_COLUMNS)} FROM sandbox_envs WHERE backend_env_id = $1 ORDER BY created_at DESC", (backend_env_id,)
                )
            else:
                rows = await tx.query(
                    f"SELECT {', '.join(_COLUMNS)} FROM sandbox_envs WHERE backend = $1 AND backend_env_id = $2 ORDER BY created_at DESC",
                    (backend, backend_env_id),
                )
        return EnvRecord.from_row(owner, rows[0]) if rows else None

    async def active(self, owner: str) -> list[EnvRecord]:
        await self.migrate()
        async with self.bind(owner).transaction(read_only=True) as tx:
            rows = await tx.query(f"SELECT {', '.join(_COLUMNS)} FROM sandbox_envs WHERE state IN ('creating', 'ready', 'destroying') ORDER BY created_at")
        return [EnvRecord.from_row(owner, r) for r in rows]

    async def directory(self, *, active_only: bool = True) -> list[DirectoryEntry]:
        """平台目录：环境属于哪个作用域（对账用）。"""
        await self.migrate()
        sql = "SELECT env_ref, owner, backend, state, updated_at FROM sandbox_env_directory"
        if active_only:
            sql += " WHERE state IN ('creating', 'ready', 'destroying')"
        async with self.bind(PLATFORM_ID).transaction(read_only=True) as tx:
            rows = await tx.query(sql + " ORDER BY updated_at")
        return [DirectoryEntry(**{k: r[k] for k in ("env_ref", "owner", "backend", "state", "updated_at")}) for r in rows]

    # --- 用量 ---

    async def usage(self, owner: str, day: str) -> Usage:
        await self.migrate()
        async with self.bind(owner).transaction(read_only=True) as tx:
            row = await tx.query_one("SELECT day, sandbox_seconds, egress_bytes, mirror_miss_bytes, cost_usd FROM sandbox_usage WHERE day = $1", (day,))
        return Usage(**row) if row else Usage(day=day)

    async def add_usage(
        self, owner: str, when: datetime, *, sandbox_seconds: int = 0, egress_bytes: int = 0, mirror_miss_bytes: int = 0, cost_usd: float = 0.0
    ) -> None:
        if owner == PLATFORM_ID:
            return  # 平台作用域的环境不计入任何租户
        await self.migrate()
        async with self.bind(owner).transaction() as tx:
            await tx.execute(
                "INSERT INTO sandbox_usage (day, sandbox_seconds, egress_bytes, mirror_miss_bytes, cost_usd) VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (day) DO UPDATE SET sandbox_seconds = sandbox_usage.sandbox_seconds + excluded.sandbox_seconds, "
                "egress_bytes = sandbox_usage.egress_bytes + excluded.egress_bytes, "
                "mirror_miss_bytes = sandbox_usage.mirror_miss_bytes + excluded.mirror_miss_bytes, "
                "cost_usd = sandbox_usage.cost_usd + excluded.cost_usd",
                (when.astimezone(UTC).strftime("%Y-%m-%d"), int(sandbox_seconds), int(egress_bytes), int(mirror_miss_bytes), float(cost_usd)),
            )

    # --- 内部 ---

    async def _update(self, record: EnvRecord, columns: Sequence[str]) -> None:
        await self.migrate()
        sets = ", ".join(f"{c} = ${i}" for i, c in enumerate(columns, start=1))
        async with self.bind(record.owner).transaction() as tx:
            await tx.execute(f"UPDATE sandbox_envs SET {sets} WHERE env_ref = ${len(columns) + 1}", [*[getattr(record, c) for c in columns], record.env_ref])

    async def _directory(self, record: EnvRecord, state: str) -> None:
        await self.migrate()
        async with self.bind(PLATFORM_ID).transaction() as tx:
            await tx.execute(
                "INSERT INTO sandbox_env_directory (env_ref, owner, backend, state, updated_at) VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (env_ref) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at, backend = excluded.backend",
                (record.env_ref, record.owner, record.backend, state, iso(self._clock())),
            )


def check_owner(owner: str) -> str:
    if owner != PLATFORM_ID and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", owner):
        raise InvalidError("malformed_tenant", "作用域的格式不对")
    return owner


__all__ = ["ACTIVE", "MIGRATIONS", "check_owner", "TERMINAL", "DirectoryEntry", "EnvRecord", "Quota", "SandboxLedger", "Usage", "iso", "parse_iso", "utc_now"]
