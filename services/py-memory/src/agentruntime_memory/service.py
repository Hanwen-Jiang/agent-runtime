"""Memory Service 的核心（设计书 3.4.1、附录 I.4–I.7；计划“Memory Service 主体”、S-12b、S-13）。

存储：
- 条目的元数据、谱系、墓碑、人审队列、修订记录与审计日志在 RelationalStore（生产是 Postgres 行级安全，
  开发是每个作用域一个 SQLite 库）；条目正文按租户密钥 AES-256-GCM 加密（密钥环用途 memory_body），可过滤的元数据明文。
  墓碑与错误签名存带密钥的散列，不存原文。
- 向量在 VectorStore 的 memory 语料（生产是 Qdrant：租户字段分区、强制过滤、保距变换）。平台级经验在平台作用域，只读。

写入（异步）：submit 校验并持久地排进待处理队列后立即返回；drain 处理队列：
schema → 安全过滤（safety）→ 墓碑 → 合并（重复的加证据、版本加一；与高信任条目冲突的进人审）→ 落库并建索引 →
自动晋升（promotion）→ 审计。每次变更让租户的修订号加一，并记下前后快照（加密），供回滚。

检索：查询构造（按时机）→ 结构化预过滤（作用域 ACL 由身份拼接、kind、任务族、工具、有效、未被取代、状态 active）→
出错时先按错误签名精确匹配 → 稠密 + 稀疏混合召回（RRF）→ 重排（查询词覆盖率 + 稠密相似度）→
业务打分（相关 × 信任 × 新鲜度 × 作用域权重）→ MMR 去冗余 → 按 token 预算截断。返回的条目带来源。

删除：沿 lineage 级联（传递闭包），条目与向量一并删除，写墓碑（内容散列）防止同一内容再写进来，
并清掉修订记录里这些条目的快照（删除是遗忘请求）。
回滚：把修订号之后的变更按相反顺序撤销并重建索引；用户删除的条目不会被回滚恢复。

冲突的判定：同一作用域、同一 kind，标题（适用情形）的词面相似度达到 conflict_threshold，指导不同，且已有条目的信任
不低于 TRUSTED。重复的判定：嵌入的余弦相似度达到 duplicate_threshold。
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import math
import os
import re
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from functools import cache
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from jsonschema import Draft202012Validator
from jsonschema_specifications import REGISTRY as SPECIFICATIONS
from referencing import Registry, Resource

from agentruntime_core.adapters.keyring import KeySource
from agentruntime_core.clock import Clock, SystemClock
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError
from agentruntime_core.providers.relational_store import Migration, RelationalStore, RelationalStoreProvider, Transaction
from agentruntime_core.providers.vector_store import AnyOf, Filter, VectorRecord, VectorStore, VectorStoreProvider
from agentruntime_core.tenancy import Scope, platform_scope, tenant_scope

from agentruntime_memory.embedding import Embedder, embedding_text, sparse_vector, tokens
from agentruntime_memory.identity import Identity
from agentruntime_memory.promotion import Evidence, PromotionPolicy
from agentruntime_memory.safety import EXTERNAL_TRUST_CAP, screen
from agentruntime_memory.scopes import SCOPE_WEIGHTS, access_token, can_delete, can_read, can_write, level_of, read_tokens

CORPUS = "memory"
TRIGGERS = ("planning", "node_start", "error")
FRESHNESS_HALF_LIFE_DAYS = 90
KINDS = ("preference", "success", "failure", "fact", "recipe")
BODY_PURPOSE = "memory_body"
PLATFORM_PURPOSE = "memory"
TRUSTED = 0.8
"""冲突判定里“高信任条目”的下限。"""
MIN_RELEVANCE = 0.35
"""重排分低于它的候选不返回（与查询无关的条目不注入）。"""
REDUNDANT = 0.9
"""MMR：与已选条目的嵌入相似度达到它的候选视为冗余。"""
MMR_LAMBDA = 0.7
MAX_TOP_K = 50
SCHEMA_ID = "https://agentruntime.example/schemas/memory_item.schema.json"
SCHEMAS_DIR = Path(__file__).resolve().parents[4] / "schemas"
SCOPE_KEYS = ("tenant", "workspace", "user", "session", "task")
# 手动晋升时，各目标层级接受的来源层级。
PROMOTION_SOURCES = {"user": ("session", "task"), "workspace": ("session", "task", "user_workspace"), "tenant": ("workspace",), "platform": ("tenant",)}

MIGRATIONS = (
    Migration(
        version=100,
        name="memory_tables",
        statements=(
            "CREATE TABLE memory_items (id TEXT PRIMARY KEY, level TEXT NOT NULL, acl TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, "
            "source TEXT NOT NULL, trust DOUBLE PRECISION NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT, superseded_by TEXT, "
            "families TEXT NOT NULL, tools TEXT NOT NULL, content_hash TEXT NOT NULL, signature TEXT NOT NULL, version BIGINT NOT NULL, "
            "updated_at TEXT NOT NULL, body TEXT NOT NULL)",
            "CREATE INDEX memory_items_signature ON memory_items (signature)",
            "CREATE TABLE memory_lineage (child TEXT NOT NULL, parent TEXT NOT NULL, PRIMARY KEY (child, parent))",
            "CREATE INDEX memory_lineage_parent ON memory_lineage (parent)",
            "CREATE TABLE memory_queue (seq BIGINT PRIMARY KEY, submitted_at TEXT NOT NULL, body TEXT NOT NULL)",
            "CREATE TABLE memory_reviews (id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE memory_tombstones (content_hash TEXT PRIMARY KEY, memory_id TEXT NOT NULL, deleted_at TEXT NOT NULL)",
            "CREATE TABLE memory_revisions (revision BIGINT PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, op TEXT NOT NULL, "
            "memory_id TEXT NOT NULL, before_state TEXT NOT NULL, after_state TEXT NOT NULL)",
            "CREATE TABLE memory_counters (name TEXT PRIMARY KEY, value BIGINT NOT NULL)",
            "CREATE TABLE memory_audit (id TEXT PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, op TEXT NOT NULL, memory_id TEXT NOT NULL, detail TEXT NOT NULL)",
        ),
    ),
)

_COLUMNS = (
    "id", "level", "acl", "kind", "status", "source", "trust", "valid_from", "valid_to", "superseded_by",
    "families", "tools", "content_hash", "signature", "version", "updated_at", "body",
)  # fmt: skip
_UPSERT = (
    f"INSERT INTO memory_items ({', '.join(_COLUMNS)}) VALUES ({', '.join(f'${i + 1}' for i in range(len(_COLUMNS)))}) "
    f"ON CONFLICT (id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in _COLUMNS[1:])}"
)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


@dataclass(frozen=True)
class SearchQuery:
    trigger: str
    query: str = ""
    task_family: str = ""
    tools: tuple[str, ...] = ()
    error_signature: str = ""
    kinds: tuple[str, ...] = ()
    token_budget: int = 0
    top_k: int = 10


@dataclass(frozen=True)
class Retrieved:
    item: dict[str, Any]
    score: float


@dataclass(frozen=True)
class Feedback:
    memory_id: str
    adopted: bool
    task_succeeded: bool


@dataclass
class DrainReport:
    accepted: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    """被拒绝的候选的原因（不含原文）。"""
    promoted: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReviewItem:
    review_id: str
    memory_id: str
    reason: str
    item: dict[str, Any]


@dataclass(frozen=True)
class RollbackReport:
    from_revision: int
    to_revision: int
    reverted: list[str] = field(default_factory=list)
    kept_deleted: list[str] = field(default_factory=list)


# --- 校验与归一 ---


@cache
def _validator() -> Draft202012Validator:
    resources = []
    for path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
        schema = json.loads(path.read_text("utf-8"))
        resources.append((schema["$id"], Resource.from_contents(schema)))
    registry: Registry = SPECIFICATIONS.with_resources(resources)
    schema = json.loads((SCHEMAS_DIR / "memory_item.schema.json").read_text("utf-8"))
    return Draft202012Validator(schema, registry=registry, format_checker=Draft202012Validator.FORMAT_CHECKER)


def schema_valid(item: Any) -> bool:
    return isinstance(item, dict) and _validator().is_valid(item)


def _integral(item: dict[str, Any]) -> dict[str, Any]:
    """经 protobuf Struct 传来的整数是浮点数，换回整数。"""
    out = copy.deepcopy(item)
    out["version"] = int(out["version"])
    if isinstance(out.get("stats"), dict):
        out["stats"] = {k: int(v) for k, v in out["stats"].items()}
    return out


def _scope(item: dict[str, Any]) -> dict[str, Any]:
    return {k: item["scope"].get(k) for k in SCOPE_KEYS}


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def normalize_signature(text: str) -> str:
    """错误签名的归一化：去掉路径、地址与数字（行号、端口、计数），统一空白与大小写。"""
    s = text.lower()
    s = re.sub(r"(?:[a-z]:)?(?:[\\/][^\s'\"():,]+)+", "<path>", s)
    s = re.sub(r"0x[0-9a-f]+", "<addr>", s)
    s = re.sub(r"\d+", "#", s)
    return re.sub(r"\s+", " ", s).strip()


def title_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    ta, tb = Counter(tokens(a["content"]["title"])), Counter(tokens(b["content"]["title"]))
    dot = sum(ta[t] * tb[t] for t in ta)
    norm = math.sqrt(sum(v * v for v in ta.values())) * math.sqrt(sum(v * v for v in tb.values()))
    return dot / norm if norm else 0.0


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def estimate_tokens(item: dict[str, Any]) -> int:
    """注入一条要占的 token：英文词与数字各算一个，非 ASCII 字符各算一个，加上来源等说明的开销。"""
    text = item["content"]["title"] + " " + item["content"]["guidance"]
    return len(re.findall(r"[A-Za-z0-9_]+|[^\x00-\x7f]", text)) + 12


def _verified(item: dict[str, Any]) -> int:
    return sum(1 for e in item.get("evidence", []) if e.get("outcome") == "verified")


# --- 作用域上下文：存储、向量与密钥 ---


@dataclass
class _Context:
    scope: Scope
    store: RelationalStore
    vectors: VectorStore
    key: bytes

    @property
    def owner(self) -> str:
        return "_platform" if self.scope.is_platform else self.scope.tenant_id

    def _derived(self, label: bytes) -> bytes:
        return hmac.new(self.key, label, hashlib.sha256).digest()

    def seal(self, data: Any, aad: str) -> str:
        nonce = os.urandom(12)
        sealed = AESGCM(self._derived(b"aes-256-gcm")).encrypt(nonce, json.dumps(data, ensure_ascii=False).encode(), aad.encode())
        return base64.b64encode(nonce + sealed).decode()

    def open(self, text: str, aad: str) -> Any:
        raw = base64.b64decode(text)
        return json.loads(AESGCM(self._derived(b"aes-256-gcm")).decrypt(raw[:12], raw[12:], aad.encode()))

    def digest(self, label: str, text: str) -> str:
        return hmac.new(self._derived(b"digest"), f"{label}\x00{text}".encode(), hashlib.sha256).hexdigest()


def _envelope(item: dict[str, Any], status: str = "active", members: Sequence[str] = (), review: str = "") -> dict[str, Any]:
    return {"item": item, "status": status, "members": sorted({m for m in members if m}), "review": review}


class MemoryService:
    def __init__(
        self,
        *,
        relational: RelationalStoreProvider,
        vectors: VectorStoreProvider,
        keyring: KeySource,
        embedder: Embedder,
        policy: PromotionPolicy | None = None,
        clock: Clock | None = None,
        duplicate_threshold: float = 0.95,
        conflict_threshold: float = 0.85,
    ) -> None:
        self.relational = relational
        self.vectors = vectors
        self.keyring = keyring
        self.embedder = embedder
        self.policy = policy or PromotionPolicy()
        self.clock = clock or SystemClock()
        self.duplicate_threshold = duplicate_threshold
        self.conflict_threshold = conflict_threshold
        self._locks: dict[str, asyncio.Lock] = {}

    # --- 基础设施 ---

    async def setup(self) -> None:
        """执行迁移。"""
        await self.relational.migrate(MIGRATIONS)

    async def _tenant(self, identity: Identity) -> _Context:
        tenant = identity.tenant_context()
        scope = tenant_scope(tenant)
        return _Context(scope, self.relational.bind(tenant), self.vectors.bind(tenant), await self.keyring.key(scope, BODY_PURPOSE))

    async def _platform(self) -> _Context:
        scope = platform_scope(PLATFORM_PURPOSE)
        return _Context(
            scope, self.relational.bind_platform(PLATFORM_PURPOSE), self.vectors.bind_platform(PLATFORM_PURPOSE), await self.keyring.key(scope, BODY_PURPOSE)
        )

    def _lock(self, ctx: _Context) -> asyncio.Lock:
        return self._locks.setdefault(ctx.owner, asyncio.Lock())

    def _now(self) -> datetime:
        return self.clock.now()

    def _new_id(self) -> str:
        n = (int(self._now().timestamp() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
        return "mem_" + "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in reversed(range(26)))

    def _row(self, ctx: _Context, env: dict[str, Any]) -> tuple[Any, ...]:
        item = env["item"]
        content = item["content"]
        applies = item.get("applies_when", {})
        validity = item["validity"]
        signature = ctx.digest("signature", normalize_signature(content["title"])) if item["kind"] == "failure" else ""
        return (
            item["id"],
            level_of(item["scope"]),
            access_token(item["scope"]),
            item["kind"],
            env["status"],
            item["provenance"]["source"],
            float(item["trust"]),
            validity["valid_from"],
            validity.get("valid_to"),
            validity.get("superseded_by"),
            json.dumps(applies.get("task_family", [])),
            json.dumps(applies.get("tools", [])),
            self._content_hash(ctx, item),
            signature,
            int(item["version"]),
            self._now().isoformat(),
            ctx.seal(env, f"item:{item['id']}"),
        )

    def _content_hash(self, ctx: _Context, item: dict[str, Any]) -> str:
        return ctx.digest("content", _norm_text(item["content"]["title"]) + "\n" + _norm_text(item["content"]["guidance"]))

    @staticmethod
    def _open_row(ctx: _Context, row: dict[str, Any]) -> dict[str, Any]:
        env = ctx.open(row["body"], f"item:{row['id']}")
        env["updated_at"] = row["updated_at"]
        return env

    async def _load(self, ctx: _Context, ids: Sequence[str], tx: Transaction | None = None) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        sql = f"SELECT id, body, updated_at FROM memory_items WHERE id IN ({', '.join(f'${i + 1}' for i in range(len(ids)))})"
        if tx is not None:
            rows = await tx.query(sql, tuple(ids))
        else:
            async with ctx.store.transaction(read_only=True) as t:
                rows = await t.query(sql, tuple(ids))
        return {str(r["id"]): self._open_row(ctx, dict(r)) for r in rows}

    async def _load_one(self, ctx: _Context, memory_id: str) -> dict[str, Any] | None:
        return (await self._load(ctx, [memory_id])).get(memory_id)

    async def _next(self, tx: Transaction, name: str) -> int:
        await tx.execute("INSERT INTO memory_counters (name, value) VALUES ($1, 0) ON CONFLICT (name) DO NOTHING", (name,))
        await tx.execute("UPDATE memory_counters SET value = value + 1 WHERE name = $1", (name,))
        row = await tx.query_one("SELECT value FROM memory_counters WHERE name = $1", (name,))
        assert row is not None
        return int(row["value"])

    async def _revise(self, tx: Transaction, ctx: _Context, actor: Identity, op: str, memory_id: str, before: Any, after: Any) -> int:
        """记一个修订：前后快照加密存放；快照为 None 表示条目不存在。"""
        revision = await self._next(tx, "revision")
        seal = lambda env: "" if env is None else ctx.seal({k: v for k, v in env.items() if k != "updated_at"}, f"rev:{memory_id}")  # noqa: E731
        await tx.execute(
            "INSERT INTO memory_revisions (revision, at, actor, op, memory_id, before_state, after_state) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            (revision, self._now().isoformat(), actor.user_id, op, memory_id, seal(before), seal(after)),
        )
        return revision

    async def _audit(self, tx: Transaction, actor: Identity, op: str, memory_id: str, detail: str = "") -> None:
        """审计日志：只记操作与判定依据的代码，不记条目内容。"""
        await tx.execute(
            "INSERT INTO memory_audit (id, at, actor, op, memory_id, detail) VALUES ($1, $2, $3, $4, $5, $6)",
            (uuid.uuid4().hex, self._now().isoformat(), actor.user_id, op, memory_id, detail),
        )

    async def _put(self, tx: Transaction, ctx: _Context, env: dict[str, Any]) -> None:
        item = env["item"]
        await tx.execute(_UPSERT, self._row(ctx, env))
        await tx.execute("DELETE FROM memory_lineage WHERE child = $1", (item["id"],))
        for parent in sorted(set(item.get("lineage", []))):
            await tx.execute("INSERT INTO memory_lineage (child, parent) VALUES ($1, $2) ON CONFLICT (child, parent) DO NOTHING", (item["id"], parent))
        await tx.execute("DELETE FROM memory_reviews WHERE memory_id = $1", (item["id"],))
        if env["status"] == "review":
            await tx.execute(
                "INSERT INTO memory_reviews (id, memory_id, reason, created_at) VALUES ($1, $2, $3, $4)",
                ("rv_" + uuid.uuid4().hex, item["id"], env["review"] or "review", self._now().isoformat()),
            )

    async def _remove(self, tx: Transaction, memory_id: str) -> None:
        await tx.execute("DELETE FROM memory_items WHERE id = $1", (memory_id,))
        await tx.execute("DELETE FROM memory_lineage WHERE child = $1", (memory_id,))
        await tx.execute("DELETE FROM memory_reviews WHERE memory_id = $1", (memory_id,))

    async def _embed(self, identity: Identity, item: dict[str, Any]) -> tuple[float, ...]:
        [dense] = await self.embedder.embed(identity, [embedding_text(item)])
        return dense

    async def _index(self, ctx: _Context, env: dict[str, Any], dense: Sequence[float]) -> None:
        """active 的条目进向量索引；其余从索引里拿掉。"""
        item = env["item"]
        if env["status"] != "active" or item["validity"].get("superseded_by"):
            await ctx.vectors.delete(CORPUS, [item["id"]])
            return
        applies = item.get("applies_when", {})
        fields = {
            "acl": access_token(item["scope"]),
            "level": level_of(item["scope"]),
            "kind": item["kind"],
            "families": tuple(applies.get("task_family", [])) or ("*",),
            "tools": tuple(applies.get("tools", [])) or ("*",),
        }
        record = VectorRecord(id=item["id"], dense=tuple(dense), embedding_version=item["embedding_version"], sparse=sparse_vector(embedding_text(item)), fields=fields)
        await ctx.vectors.upsert(CORPUS, [record])

    async def _tombstoned(self, ctx: _Context, content_hash: str) -> bool:
        async with ctx.store.transaction(read_only=True) as tx:
            return await tx.query_one("SELECT memory_id FROM memory_tombstones WHERE content_hash = $1", (content_hash,)) is not None

    # --- 写入 ---

    async def submit(self, identity: Identity, candidates: Sequence[tuple[dict[str, Any], str]]) -> int:
        """（条目, 来源 Task id）排进待处理队列，返回接受的条数。不属于身份可写作用域的、不合 schema 的不接受。"""
        accepted = [(_integral(item), str(task_id)) for item, task_id in candidates if schema_valid(item) and can_write(identity, item["scope"])]
        if not accepted:
            return 0
        ctx = await self._tenant(identity)
        submitter = {"user_id": identity.user_id, "workspace_id": identity.workspace_id, "session_id": identity.session_id, "roles": sorted(identity.roles)}
        async with ctx.store.transaction() as tx:
            for item, task_id in accepted:
                seq = await self._next(tx, "queue")
                body = ctx.seal({"item": item, "task_id": task_id, "identity": submitter}, f"queue:{seq}")
                await tx.execute("INSERT INTO memory_queue (seq, submitted_at, body) VALUES ($1, $2, $3)", (seq, self._now().isoformat(), body))
        return len(accepted)

    async def drain(self, identity: Identity) -> DrainReport:
        """处理身份所在租户的待处理队列。"""
        ctx = await self._tenant(identity)
        report = DrainReport()
        async with self._lock(ctx):
            async with ctx.store.transaction(read_only=True) as tx:
                rows = await tx.query("SELECT seq, body FROM memory_queue ORDER BY seq")
            for row in rows:
                entry = ctx.open(str(row["body"]), f"queue:{row['seq']}")
                who = entry["identity"]
                submitter = Identity(
                    tenant_id=identity.tenant_id, user_id=who["user_id"], workspace_id=who["workspace_id"], session_id=who["session_id"], roles=frozenset(who["roles"])
                )
                await self._process(ctx, submitter, entry["item"], report)
                async with ctx.store.transaction() as tx:
                    await tx.execute("DELETE FROM memory_queue WHERE seq = $1", (row["seq"],))
        return report

    async def _reject(self, ctx: _Context, submitter: Identity, memory_id: str, reasons: Sequence[str], report: DrainReport) -> None:
        detail = ",".join(reasons)
        report.rejected.append(f"{memory_id}：{detail}")
        async with ctx.store.transaction() as tx:
            await self._audit(tx, submitter, "reject", memory_id, detail)

    async def _process(self, ctx: _Context, submitter: Identity, candidate: dict[str, Any], report: DrainReport) -> None:
        cid = candidate["id"]
        if not schema_valid(candidate) or not can_write(submitter, candidate["scope"]):
            await self._reject(ctx, submitter, cid, ["scope_or_schema"], report)
            return
        screening = screen(candidate)
        if screening.action == "reject":
            await self._reject(ctx, submitter, cid, screening.reasons, report)
            return
        item = screening.item
        item.pop("stats", None)
        item["embedding_version"] = self.embedder.version
        item["version"] = 1
        if await self._tombstoned(ctx, self._content_hash(ctx, item)):
            await self._reject(ctx, submitter, cid, ["tombstoned"], report)
            return
        if await self._load_one(ctx, cid) is not None:
            await self._reject(ctx, submitter, cid, ["id_exists"], report)
            return
        dense = await self._embed(submitter, item)
        status, review = "active", ""
        if screening.action == "review":
            status, review = "review", ",".join(r for r in screening.reasons if r.startswith("instruction:"))
        else:
            duplicate, conflict = await self._neighbors(ctx, item, dense)
            if duplicate is not None:
                merged = await self._merge(ctx, submitter, duplicate, item)
                report.merged.append(merged["item"]["id"])
                await self._auto_promote(ctx, submitter, merged, report)
                return
            if conflict is not None:
                status, review = "review", f"conflict_with:{conflict['item']['id']}"
        env = _envelope(item, status, [submitter.user_id], review)
        await self._create(ctx, submitter, env, dense, op="create", detail=",".join(screening.reasons))
        if status == "review":
            report.review.append(cid)
            return
        report.accepted.append(cid)
        await self._auto_promote(ctx, submitter, env, report)

    async def _neighbors(self, ctx: _Context, item: dict[str, Any], dense: Sequence[float]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """同一作用域、同一 kind 的近邻里找重复（返回第一个值）与冲突（第二个值）。"""
        flt = Filter(must={"acl": access_token(item["scope"]), "level": level_of(item["scope"]), "kind": item["kind"]})
        hits = await ctx.vectors.query(CORPUS, embedding_version=item["embedding_version"], top_k=10, dense=dense, filter=flt)
        envs = await self._load(ctx, [h.id for h in hits])
        same = [(h, envs[h.id]) for h in hits if h.id in envs and envs[h.id]["status"] == "active" and _scope(envs[h.id]["item"]) == _scope(item)]
        for hit, env in same:
            if hit.score >= self.duplicate_threshold:
                return env, None
        for _, env in same:
            other = env["item"]
            if (
                float(other["trust"]) >= TRUSTED
                and title_similarity(other, item) >= self.conflict_threshold
                and _norm_text(other["content"]["guidance"]) != _norm_text(item["content"]["guidance"])
            ):
                return None, env
        return None, None

    async def _create(self, ctx: _Context, actor: Identity, env: dict[str, Any], dense: Sequence[float], *, op: str, detail: str = "") -> None:
        async with ctx.store.transaction() as tx:
            await self._put(tx, ctx, env)
            await self._revise(tx, ctx, actor, op, env["item"]["id"], None, env)
            await self._audit(tx, actor, op, env["item"]["id"], detail)
        await self._index(ctx, env, dense)

    async def _update(self, ctx: _Context, actor: Identity, before: dict[str, Any], after: dict[str, Any], *, op: str) -> None:
        async with ctx.store.transaction() as tx:
            await self._put(tx, ctx, after)
            await self._revise(tx, ctx, actor, op, after["item"]["id"], before, after)
            await self._audit(tx, actor, op, after["item"]["id"])

    async def _merge(self, ctx: _Context, submitter: Identity, env: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        """条目级增量：只加证据与谱系、版本加一、信任取较高者，不整体重写。"""
        before = copy.deepcopy(env)
        after = copy.deepcopy(env)
        item = after["item"]
        traces = {e["trace"] for e in item.get("evidence", [])}
        added = [e for e in candidate.get("evidence", []) if e["trace"] not in traces]
        if added:
            item["evidence"] = [*item.get("evidence", []), *added]
        lineage = [p for p in candidate.get("lineage", []) if p not in item.get("lineage", []) and p != item["id"]]
        if lineage:
            item["lineage"] = [*item.get("lineage", []), *lineage]
        item["version"] = int(item["version"]) + 1
        item["trust"] = max(float(item["trust"]), float(candidate["trust"]))
        after["members"] = sorted({*after["members"], submitter.user_id} - {""})
        await self._update(ctx, submitter, before, after, op="merge")
        return after

    # --- 晋升 ---

    def _evidence(self, env: dict[str, Any]) -> Evidence:
        item = env["item"]
        source = item["provenance"]["source"]
        return Evidence(
            user_explicit=source == "user_explicit",
            corrections=int(item["version"]) if source == "user_correction" else 0,
            verified_successes=_verified(item),
            distinct_members=len(env["members"]),
        )

    async def _children(self, ctx: _Context, parent: str) -> list[tuple[str, str]]:
        """（派生条目 id, 层级）。"""
        async with ctx.store.transaction(read_only=True) as tx:
            rows = await tx.query(
                "SELECT l.child AS child, i.level AS level FROM memory_lineage l JOIN memory_items i ON i.id = l.child WHERE l.parent = $1 ORDER BY l.child", (parent,)
            )
        return [(str(r["child"]), str(r["level"])) for r in rows]

    async def _auto_promote(self, ctx: _Context, submitter: Identity, env: dict[str, Any], report: DrainReport) -> None:
        """Session（及 Task）的条目满足规则时自动晋升，一次只晋升到一个层级：偏好到 User，其余优先到 Workspace。"""
        item = env["item"]
        if env["status"] != "active" or level_of(item["scope"]) not in ("session", "task"):
            return
        evidence = self._evidence(env)
        for target in ("user",) if item["kind"] == "preference" else ("workspace", "user"):
            if target == "workspace" and not submitter.workspace_id:
                continue
            if not self.policy.evaluate(item, target, evidence).allowed:
                continue
            if any(level == target for _, level in await self._children(ctx, item["id"])):
                return
            new_id = await self._promote_into(ctx, submitter, env, target)
            if new_id is not None:
                report.promoted.append(new_id)
            return

    async def _promote_into(self, ctx: _Context, actor: Identity, env: dict[str, Any], target: str) -> str | None:
        item = env["item"]
        promoted = self.policy.promoted(item, target, actor, new_id=self._new_id())
        promoted["embedding_version"] = self.embedder.version
        dest = await self._platform() if target == "platform" else ctx
        if await self._tombstoned(dest, self._content_hash(dest, promoted)):
            return None
        dense = await self._embed(actor, promoted)
        duplicate, _ = await self._neighbors(dest, promoted, dense)
        if duplicate is not None:
            return (await self._merge(dest, actor, duplicate, promoted))["item"]["id"]
        members = [] if target == "platform" else env["members"]
        await self._create(dest, actor, _envelope(promoted, "active", members), dense, op="promote", detail=f"from:{item['id']}")
        return promoted["id"]

    async def promote(self, identity: Identity, memory_id: str, target: str, evidence: Evidence | None = None) -> str | None:
        """手动晋升（要批准的层级由批准者的角色给出）；返回新条目 id，不允许时返回 None。"""
        if target not in PROMOTION_SOURCES:
            raise InvalidError("invalid_target", "晋升目标只能是 user、workspace、tenant 或 platform")
        ctx = await self._tenant(identity)
        env = await self._load_one(ctx, memory_id)
        if env is None or env["status"] != "active" or not can_read(identity, env["item"]["scope"]):
            return None
        item = env["item"]
        if level_of(item["scope"]) not in PROMOTION_SOURCES[target]:
            return None
        if target == "workspace" and not (item["scope"].get("workspace") or identity.workspace_id):
            return None
        # 批准者的角色只取认证身份的，调用方给的不算。
        decided = replace(evidence or self._evidence(env), approved_by=frozenset(identity.roles))
        if not self.policy.evaluate(item, target, decided).allowed:
            return None
        if target != "platform":
            existing = [child for child, level in await self._children(ctx, memory_id) if level == target]
            if existing:
                return existing[0]
        return await self._promote_into(ctx, identity, env, target)

    # --- 检索 ---

    def _usable(self, identity: Identity, env: dict[str, Any], query: SearchQuery, today: date) -> bool:
        item = env["item"]
        validity = item["validity"]
        applies = item.get("applies_when", {})
        families, tools = applies.get("task_family", []), applies.get("tools", [])
        return (
            env["status"] == "active"
            and not validity.get("superseded_by")
            and validity["valid_from"] <= today.isoformat()
            and (not validity.get("valid_to") or validity["valid_to"] >= today.isoformat())
            and can_read(identity, item["scope"])
            and (not query.kinds or item["kind"] in query.kinds)
            and (not query.task_family or not families or query.task_family in families)
            and (not query.tools or not tools or bool(set(query.tools) & set(tools)))
        )

    def _freshness(self, env: dict[str, Any], now: datetime) -> float:
        since = max(date.fromisoformat(env["item"]["validity"]["valid_from"]), datetime.fromisoformat(env["updated_at"]).date())
        age = max((now.date() - since).days, 0)
        return 0.5 ** (age / FRESHNESS_HALF_LIFE_DAYS)

    async def search(self, identity: Identity, query: SearchQuery) -> list[Retrieved]:
        if query.trigger not in TRIGGERS:
            raise InvalidError("invalid_trigger", "trigger 只能是 planning、node_start 或 error")
        if any(k not in KINDS for k in query.kinds):
            raise InvalidError("invalid_kind", "kinds 里有未知的记忆类型")
        top_k = min(query.top_k if query.top_k > 0 else 10, MAX_TOP_K)
        now = self._now()
        tenant, platform = await self._tenant(identity), await self._platform()
        common: dict[str, Any] = {}
        if query.kinds:
            common["kind"] = AnyOf(tuple(query.kinds))
        if query.task_family:
            common["families"] = AnyOf((query.task_family, "*"))
        if query.tools:
            common["tools"] = AnyOf((*query.tools, "*"))

        # 候选：（上下文, 条目 id）→ 是否按错误签名精确命中。
        exact: set[tuple[int, str]] = set()
        contexts = (tenant, platform)
        candidates: dict[tuple[int, str], None] = {}
        if query.trigger == "error" and query.error_signature:
            for n, ctx in enumerate(contexts):
                sig = ctx.digest("signature", normalize_signature(query.error_signature))
                async with ctx.store.transaction(read_only=True) as tx:
                    rows = await tx.query("SELECT id FROM memory_items WHERE signature = $1 AND status = 'active' ORDER BY id", (sig,))
                for r in rows:
                    exact.add((n, str(r["id"])))
                    candidates[(n, str(r["id"]))] = None
        text = " ".join(p for p in (query.query, query.task_family, *query.tools) if p)
        dense: tuple[float, ...] = ()
        if text:
            [dense] = await self.embedder.embed(identity, [text])
            sparse = sparse_vector(text)
            limit = max(top_k * 5, 50)
            for n, (ctx, acl) in enumerate(((tenant, {"acl": AnyOf(read_tokens(identity))}), (platform, {}))):
                must = {**common, **acl}
                hits = await ctx.vectors.query(
                    CORPUS, embedding_version=self.embedder.version, top_k=limit, dense=dense, sparse=sparse if sparse.indices else None, filter=Filter(must=must) if must else None
                )
                for h in hits:
                    candidates.setdefault((n, h.id), None)
        if not candidates:
            return []

        # 取回条目与向量，复核过滤条件，重排并打分。
        q_tokens = set(tokens(query.query))
        scored: list[tuple[bool, float, tuple[int, str], dict[str, Any], tuple[float, ...]]] = []
        for n, ctx in enumerate(contexts):
            ids = [i for m, i in candidates if m == n]
            if not ids:
                continue
            envs = await self._load(ctx, ids)
            vectors = {r.id: r.dense for r in await ctx.vectors.get(CORPUS, ids)}
            for memory_id, env in envs.items():
                if not self._usable(identity, env, query, now.date()):
                    continue
                item = env["item"]
                vec = vectors.get(memory_id, ())
                is_exact = (n, memory_id) in exact
                coverage = len(q_tokens & set(tokens(embedding_text(item)))) / len(q_tokens) if q_tokens else 1.0
                relevance = 1.0 if is_exact else 0.75 * coverage + 0.25 * max(_cosine(dense, vec), 0.0) if dense else coverage
                if relevance < MIN_RELEVANCE:
                    continue
                score = relevance * float(item["trust"]) * self._freshness(env, now) * SCOPE_WEIGHTS[level_of(item["scope"])]
                if score > 0:
                    scored.append((is_exact, score, (n, memory_id), item, vec))
        scored.sort(key=lambda s: (not s[0], -s[1], s[2]))

        # MMR：精确命中的排在前面；其余按分数与和已选条目的相似度挑选，跳过冗余的。
        selected = [s for s in scored if s[0]][:top_k]
        pool = [s for s in scored if not s[0]]
        best = max((s[1] for s in pool), default=1.0) or 1.0
        while pool and len(selected) < top_k:
            choice, value = None, -math.inf
            for s in pool:
                similarity = max((_cosine(s[4], t[4]) for t in selected), default=0.0)
                if similarity >= REDUNDANT:
                    continue
                mmr = MMR_LAMBDA * s[1] / best - (1 - MMR_LAMBDA) * similarity
                if mmr > value:
                    choice, value = s, mmr
            if choice is None:
                break
            selected.append(choice)
            pool.remove(choice)

        results: list[Retrieved] = []
        used = 0
        for _, score, _, item, _ in selected:
            cost = estimate_tokens(item)
            if query.token_budget > 0 and used + cost > query.token_budget:
                break
            used += cost
            results.append(Retrieved(item=copy.deepcopy(item), score=score))
        return results

    # --- 反馈、读取、删除 ---

    async def feedback(self, identity: Identity, task_id: str, feedback: Sequence[Feedback]) -> None:
        """用后反馈：采用且 Task 成功 helped 加一、信任略升；采用但失败 hurt 加一、信任下降。看不到的条目忽略。"""
        ctx = await self._tenant(identity)
        for fb in feedback:
            env = await self._load_one(ctx, fb.memory_id)
            if env is None or env["status"] != "active" or not can_read(identity, env["item"]["scope"]):
                continue
            after = copy.deepcopy(env)
            item = after["item"]
            stats = {"retrieved": 0, "helped": 0, "hurt": 0, **item.get("stats", {})}
            stats["retrieved"] += 1
            trust = float(item["trust"])
            if fb.adopted and fb.task_succeeded:
                stats["helped"] += 1
                cap = EXTERNAL_TRUST_CAP if item["provenance"]["source"] == "external_content" else 1.0
                trust = min(cap, trust + 0.02)
            elif fb.adopted:
                stats["hurt"] += 1
                trust = max(0.0, trust - 0.1)
            item["stats"], item["trust"] = stats, round(trust, 6)
            await self._update(ctx, identity, env, after, op="feedback")

    async def get(self, identity: Identity, memory_id: str) -> dict[str, Any] | None:
        """身份看得到的条目（含待人审的，供有审核权的人查看）；看不到或不存在时返回 None。"""
        env = await self._load_one(await self._tenant(identity), memory_id)
        if env is not None:
            scope = env["item"]["scope"]
            if (env["status"] == "active" and can_read(identity, scope)) or (env["status"] == "review" and can_delete(identity, scope)):
                return copy.deepcopy(env["item"])
            return None
        env = await self._load_one(await self._platform(), memory_id)
        return copy.deepcopy(env["item"]) if env is not None and env["status"] == "active" else None

    async def _visible(self, identity: Identity, ctx: _Context, memory_id: str) -> dict[str, Any]:
        """身份在本租户看得到的条目；平台条目：DeniedError（经租户接口只读）；其余：NotFoundError。"""
        env = await self._load_one(ctx, memory_id)
        if env is not None:
            scope = env["item"]["scope"]
            if can_read(identity, scope) or (env["status"] == "review" and can_delete(identity, scope)):
                return env
        elif await self._load_one(await self._platform(), memory_id) is not None:
            raise DeniedError("platform_read_only", "平台条目经租户接口只读")
        raise NotFoundError("memory_not_found", "条目不存在或不可见")

    async def _closure(self, ctx: _Context, root: str) -> list[str]:
        """root 与沿 lineage 派生出的全部条目（传递闭包），按发现顺序。"""
        found, frontier = [root], [root]
        async with ctx.store.transaction(read_only=True) as tx:
            while frontier:
                parent = frontier.pop(0)
                for row in await tx.query("SELECT child FROM memory_lineage WHERE parent = $1 ORDER BY child", (parent,)):
                    child = str(row["child"])
                    if child not in found:
                        found.append(child)
                        frontier.append(child)
        return found

    async def _forget(self, ctx: _Context, actor: Identity, ids: Sequence[str], *, op: str, detail: str) -> None:
        """删除条目与向量，写墓碑，清掉修订记录里的快照（遗忘请求，回滚也不恢复）。"""
        async with ctx.store.transaction() as tx:
            rows = await tx.query(
                f"SELECT id, content_hash FROM memory_items WHERE id IN ({', '.join(f'${i + 1}' for i in range(len(ids)))})", tuple(ids)
            )
            for row in rows:
                memory_id = str(row["id"])
                await self._remove(tx, memory_id)
                await tx.execute(
                    "INSERT INTO memory_tombstones (content_hash, memory_id, deleted_at) VALUES ($1, $2, $3) ON CONFLICT (content_hash) DO NOTHING",
                    (row["content_hash"], memory_id, self._now().isoformat()),
                )
                await tx.execute("UPDATE memory_revisions SET before_state = '', after_state = '' WHERE memory_id = $1", (memory_id,))
                await self._revise(tx, ctx, actor, op, memory_id, None, None)
                await self._audit(tx, actor, op, memory_id, detail)
        await ctx.vectors.delete(CORPUS, list(ids))

    async def delete(self, identity: Identity, memory_id: str, reason: str = "") -> list[str]:
        """沿 lineage 级联删除，返回删掉的条目 id；看不到的条目：NotFoundError；没有删除权限：DeniedError。"""
        ctx = await self._tenant(identity)
        env = await self._visible(identity, ctx, memory_id)
        if not can_delete(identity, env["item"]["scope"]):
            raise DeniedError("delete_denied", "没有删除这个条目的权限")
        ids = await self._closure(ctx, memory_id)
        await self._forget(ctx, identity, ids, op="delete", detail="reason_given" if reason else "")
        return ids

    # --- 人审 ---

    async def reviews(self, identity: Identity) -> list[ReviewItem]:
        """身份有审核权的待审条目：本人的条目由本人审，Workspace 的由 maintainer，Tenant 的由 admin。"""
        ctx = await self._tenant(identity)
        async with ctx.store.transaction(read_only=True) as tx:
            rows = await tx.query("SELECT id, memory_id, reason FROM memory_reviews ORDER BY created_at, id")
        envs = await self._load(ctx, [str(r["memory_id"]) for r in rows])
        out = []
        for r in rows:
            env = envs.get(str(r["memory_id"]))
            if env is not None and can_delete(identity, env["item"]["scope"]):
                out.append(ReviewItem(review_id=str(r["id"]), memory_id=str(r["memory_id"]), reason=str(r["reason"]), item=copy.deepcopy(env["item"])))
        return out

    async def resolve_review(self, identity: Identity, review_id: str, *, approve: bool) -> None:
        """通过：条目生效并建索引；拒绝：条目删除并写墓碑。"""
        ctx = await self._tenant(identity)
        async with ctx.store.transaction(read_only=True) as tx:
            row = await tx.query_one("SELECT memory_id FROM memory_reviews WHERE id = $1", (review_id,))
        env = await self._load_one(ctx, str(row["memory_id"])) if row is not None else None
        if env is None or not can_delete(identity, env["item"]["scope"]):
            if env is not None and can_read(identity, env["item"]["scope"]):
                raise DeniedError("review_denied", "没有审核这个条目的权限")
            raise NotFoundError("review_not_found", "待审条目不存在或不可见")
        if not approve:
            await self._forget(ctx, identity, [env["item"]["id"]], op="reject", detail="review_rejected")
            return
        after = copy.deepcopy(env)
        after["status"], after["review"] = "active", ""
        await self._update(ctx, identity, env, after, op="approve")
        await self._index(ctx, after, await self._embed(identity, after["item"]))

    # --- 版本与回滚 ---

    async def revision(self, identity: Identity) -> int:
        ctx = await self._tenant(identity)
        async with ctx.store.transaction(read_only=True) as tx:
            row = await tx.query_one("SELECT value FROM memory_counters WHERE name = 'revision'")
        return int(row["value"]) if row is not None else 0

    async def rollback(self, identity: Identity, to_revision: int) -> RollbackReport:
        """要 admin。"""
        if "admin" not in identity.roles:
            raise DeniedError("rollback_denied", "回滚要 admin")
        ctx = await self._tenant(identity)
        async with self._lock(ctx):
            current = await self.revision(identity)
            async with ctx.store.transaction(read_only=True) as tx:
                rows = await tx.query("SELECT revision, op, memory_id, before_state FROM memory_revisions WHERE revision > $1 ORDER BY revision DESC", (to_revision,))
                forgotten = {str(r["memory_id"]) for r in await tx.query("SELECT memory_id FROM memory_tombstones")}
            forgotten |= {str(r["memory_id"]) for r in rows if r["op"] in ("delete", "reject")}
            reverted: list[str] = []
            kept: list[str] = []
            for row in rows:
                memory_id = str(row["memory_id"])
                if not memory_id:
                    continue
                if memory_id in forgotten:
                    if memory_id not in kept:
                        kept.append(memory_id)
                    continue
                before = ctx.open(str(row["before_state"]), f"rev:{memory_id}") if row["before_state"] else None
                now_env = await self._load_one(ctx, memory_id)
                async with ctx.store.transaction() as tx:
                    if before is None:
                        await self._remove(tx, memory_id)
                    else:
                        await self._put(tx, ctx, before)
                    await self._revise(tx, ctx, identity, "rollback", memory_id, now_env, before)
                if before is None:
                    await ctx.vectors.delete(CORPUS, [memory_id])
                else:
                    await self._index(ctx, before, await self._embed(identity, before["item"]))
                if memory_id not in reverted:
                    reverted.append(memory_id)
            async with ctx.store.transaction() as tx:
                await self._revise(tx, ctx, identity, "rollback", "", None, None)
                await self._audit(tx, identity, "rollback", "", f"to:{to_revision}")
        return RollbackReport(from_revision=current, to_revision=to_revision, reverted=reverted, kept_deleted=kept)
