"""Platform-scoped EvolutionStore for candidate storage, admin promotion, and rollback (MAT-HARNESS-ISO)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from agentruntime.registry.v1 import registry_pb2 as rg
from agentruntime_core.errors import ConflictError, DeniedError, InvalidError, NotFoundError
from agentruntime_core.providers.relational_store import Migration, RelationalStoreProvider, Row

from agentruntime_learning.evolution.isolation import EVOLUTION_PLATFORM_PURPOSE

CANDIDATE_STATUSES = frozenset({"pending_eval", "rejected_by_gate", "pending_approval", "promoted", "rolled_back"})
_SYSTEM_ACTORS = frozenset({"", "evolution-harness", "learning-pipeline", "system", "auto"})

EVOLUTION_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=301,
        name="evolution_candidates",
        statements=(
            """CREATE TABLE evolution_candidates (
                candidate_id TEXT PRIMARY KEY,
                target_kind TEXT NOT NULL,
                target_name TEXT NOT NULL,
                status TEXT NOT NULL,
                baseline_version TEXT NOT NULL,
                baseline_document TEXT NOT NULL,
                candidate_document TEXT NOT NULL,
                unified_diff TEXT NOT NULL,
                cluster_summary TEXT NOT NULL,
                matrix_report TEXT NOT NULL,
                staged_skill_name TEXT NOT NULL,
                promoted_version TEXT NOT NULL,
                rollback_version TEXT NOT NULL,
                approved_by TEXT NOT NULL,
                created_at BIGINT NOT NULL,
                updated_at BIGINT NOT NULL
            )""",
        ),
    ),
)


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _loads_dict(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    val = json.loads(raw)
    return val if isinstance(val, dict) else {}


def _dumps(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class CandidateRecord:
    candidate_id: str
    target_kind: str
    target_name: str
    status: str
    baseline_version: str
    baseline_document: dict[str, Any]
    candidate_document: dict[str, Any]
    unified_diff: str
    cluster_summary: dict[str, Any]
    matrix_report: dict[str, Any]
    staged_skill_name: str = ""
    promoted_version: str = ""
    rollback_version: str = ""
    approved_by: str = ""
    created_at: int = 0
    updated_at: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _from_row(row: Row) -> CandidateRecord:
    return CandidateRecord(
        candidate_id=str(row["candidate_id"]),
        target_kind=str(row["target_kind"]),
        target_name=str(row["target_name"]),
        status=str(row["status"]),
        baseline_version=str(row["baseline_version"]),
        baseline_document=_loads_dict(str(row["baseline_document"])),
        candidate_document=_loads_dict(str(row["candidate_document"])),
        unified_diff=str(row["unified_diff"]),
        cluster_summary=_loads_dict(str(row["cluster_summary"])),
        matrix_report=_loads_dict(str(row["matrix_report"])),
        staged_skill_name=str(row["staged_skill_name"]),
        promoted_version=str(row["promoted_version"]),
        rollback_version=str(row["rollback_version"]),
        approved_by=str(row["approved_by"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def _require_human_admin(actor: str) -> str:
    clean = actor.strip()
    if clean.lower() in _SYSTEM_ACTORS:
        raise DeniedError("admin_approval_required", f"审批或回滚必须由真实管理员触发，拒绝身份 {actor!r}")
    return clean


class EvolutionStore:
    """Platform-isolated candidate ledger for admin review, promotion, and 1-click rollback."""

    def __init__(self, provider: RelationalStoreProvider) -> None:
        self._provider = provider
        self._db = provider.bind_platform(EVOLUTION_PLATFORM_PURPOSE)

    async def migrate(self) -> None:
        await self._provider.migrate(EVOLUTION_MIGRATIONS)

    async def create_candidate(
        self,
        *,
        target_kind: str,
        target_name: str,
        baseline_version: str,
        baseline_document: Mapping[str, Any],
        candidate_document: Mapping[str, Any],
        unified_diff: str,
        cluster_summary: Mapping[str, Any],
        matrix_report: Mapping[str, Any] | None = None,
        status: str = "pending_eval",
        staged_skill_name: str = "",
        candidate_id: str | None = None,
    ) -> CandidateRecord:
        if status not in CANDIDATE_STATUSES:
            raise InvalidError("invalid_status", f"不支持的候选状态：{status}")
        cid = candidate_id or f"cand_{uuid.uuid4().hex[:12]}"
        ts = _now_ms()
        async with self._db.transaction() as tx:
            await tx.execute(
                """INSERT INTO evolution_candidates (
                    candidate_id, target_kind, target_name, status, baseline_version,
                    baseline_document, candidate_document, unified_diff, cluster_summary,
                    matrix_report, staged_skill_name, promoted_version, rollback_version,
                    approved_by, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, '', '', '', $12, $13)""",
                (
                    cid,
                    target_kind,
                    target_name,
                    status,
                    baseline_version,
                    _dumps(baseline_document),
                    _dumps(candidate_document),
                    unified_diff,
                    _dumps(cluster_summary),
                    _dumps(matrix_report or {}),
                    staged_skill_name,
                    ts,
                    ts,
                ),
            )
        return await self.get_candidate(cid)

    async def update_evaluation(
        self,
        candidate_id: str,
        *,
        matrix_report: Mapping[str, Any],
        status: str,
        staged_skill_name: str | None = None,
    ) -> CandidateRecord:
        if status not in CANDIDATE_STATUSES:
            raise InvalidError("invalid_status", f"不支持的候选状态：{status}")
        ts = _now_ms()
        async with self._db.transaction() as tx:
            row = await tx.query_one(
                "SELECT staged_skill_name FROM evolution_candidates WHERE candidate_id = $1",
                (candidate_id,),
            )
            if row is None:
                raise NotFoundError("candidate_not_found", f"找不到候选 {candidate_id}")
            staged = staged_skill_name if staged_skill_name is not None else str(row["staged_skill_name"])
            await tx.execute(
                """UPDATE evolution_candidates
                   SET status = $1, matrix_report = $2, staged_skill_name = $3, updated_at = $4
                   WHERE candidate_id = $5""",
                (status, _dumps(matrix_report), staged, ts, candidate_id),
            )
        return await self.get_candidate(candidate_id)

    async def get_candidate(self, candidate_id: str) -> CandidateRecord:
        async with self._db.transaction(read_only=True) as tx:
            row = await tx.query_one(
                "SELECT * FROM evolution_candidates WHERE candidate_id = $1",
                (candidate_id,),
            )
        if row is None:
            raise NotFoundError("candidate_not_found", f"找不到候选 {candidate_id}")
        return _from_row(row)

    async def list_candidates(self, *, status: str = "", target_name: str = "") -> list[CandidateRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            params.append(status)
            clauses.append(f"status = ${len(params)}")
        if target_name:
            params.append(target_name)
            clauses.append(f"target_name = ${len(params)}")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM evolution_candidates{where} ORDER BY created_at DESC, candidate_id DESC"
        async with self._db.transaction(read_only=True) as tx:
            rows = await tx.query(sql, tuple(params))
        return [_from_row(r) for r in rows]

    async def approve_and_promote(self, candidate_id: str, *, approved_by: str, registry: Any) -> CandidateRecord:
        admin = _require_human_admin(approved_by)
        record = await self.get_candidate(candidate_id)
        if record.status != "pending_approval":
            raise ConflictError(
                "candidate_not_promotable",
                f"候选 {candidate_id} 当前状态为 {record.status}，仅 pending_approval 允许晋升",
            )
        gate = record.matrix_report.get("gate") or {}
        if gate.get("passed") is not True:
            raise ConflictError("candidate_not_promotable", f"候选 {candidate_id} 未通过评测门槛，禁止晋升")
        if record.target_kind != "skill":
            raise InvalidError("unsupported_target_kind", f"暂不支持自动晋升种类：{record.target_kind}")

        resp = await registry.publish(
            None,
            rg.REGISTRY_KIND_SKILL,
            record.target_name,
            record.candidate_document,
            change_note=f"evolution promote {candidate_id} approved by {admin}",
        )
        ts = _now_ms()
        async with self._db.transaction() as tx:
            await tx.execute(
                """UPDATE evolution_candidates
                   SET status = 'promoted', promoted_version = $1, approved_by = $2, updated_at = $3
                   WHERE candidate_id = $4""",
                (str(resp.version), admin, ts, candidate_id),
            )
        return await self.get_candidate(candidate_id)

    async def rollback(self, candidate_id: str, *, actor: str, registry: Any, reason: str = "") -> CandidateRecord:
        admin = _require_human_admin(actor)
        record = await self.get_candidate(candidate_id)
        if record.status != "promoted":
            raise ConflictError(
                "candidate_not_promoted",
                f"候选 {candidate_id} 当前状态为 {record.status}，仅已晋升候选可回滚",
            )
        if not record.baseline_document:
            raise ConflictError("missing_baseline", f"候选 {candidate_id} 缺少基线快照，无法自动回滚")

        note = f"evolution rollback {candidate_id} by {admin}"
        if reason:
            note += f": {reason}"
        resp = await registry.publish(
            None,
            rg.REGISTRY_KIND_SKILL,
            record.target_name,
            record.baseline_document,
            change_note=note,
        )
        ts = _now_ms()
        async with self._db.transaction() as tx:
            await tx.execute(
                """UPDATE evolution_candidates
                   SET status = 'rolled_back', rollback_version = $1, updated_at = $2
                   WHERE candidate_id = $3""",
                (str(resp.version), ts, candidate_id),
            )
        return await self.get_candidate(candidate_id)
