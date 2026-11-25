"""Isolated self-evolution harness guards for tenant sys-evolution (MAT-HARNESS-ISO)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from agentruntime.registry.v1 import registry_pb2 as rg
from agentruntime_core.errors import DeniedError, InvalidError
from agentruntime_core.tenancy import TenantContext, tenant_scope

from agentruntime_worker.activities.types import Tenant

SYSTEM_EVOLUTION_TENANT = "sys-evolution"
SYSTEM_EVOLUTION_USER = "evolution-harness"
EVOLUTION_PLATFORM_PURPOSE = "evolution"
CANDIDATE_SKILL_PREFIX = "cand-"

_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SAFE_SEGMENT_RE = re.compile(r"[^a-z0-9_-]+")


def evolution_tenant_context(workspace_id: str = "eval-ws") -> TenantContext:
    """Build and validate a TenantContext locked to the isolated sys-evolution tenant."""
    ctx = TenantContext(
        tenant_id=SYSTEM_EVOLUTION_TENANT,
        user_id=SYSTEM_EVOLUTION_USER,
        workspace_id=workspace_id,
    )
    tenant_scope(ctx)
    return ctx


def evolution_worker_tenant(workspace_id: str = "eval-ws") -> Tenant:
    """Build a worker Tenant locked to sys-evolution."""
    evolution_tenant_context(workspace_id)
    return Tenant(
        tenant_id=SYSTEM_EVOLUTION_TENANT,
        user_id=SYSTEM_EVOLUTION_USER,
        workspace_id=workspace_id,
    )


def assert_evolution_tenant(tenant: Any) -> str:
    """Verify tenant belongs strictly to sys-evolution; rejects user tenants or malformed slugs."""
    if tenant is None:
        raise DeniedError("evolution_tenant_violation", "演化请求缺少租户上下文")
    tid = getattr(tenant, "tenant_id", "") or (tenant.get("tenant_id") if isinstance(tenant, Mapping) else "")
    if not tid:
        raise DeniedError("evolution_tenant_violation", "演化请求缺少 tenant_id")
    if tid != SYSTEM_EVOLUTION_TENANT:
        raise DeniedError(
            "evolution_tenant_violation",
            f"系统自进化只能运行在隔离租户 {SYSTEM_EVOLUTION_TENANT}，拒绝访问 {tid!r}",
        )
    if isinstance(tenant, TenantContext):
        tenant_scope(tenant)
    return tid


def candidate_skill_name(base_skill: str, document: Mapping[str, Any]) -> str:
    """Deterministic Registry-safe candidate skill name (<prefix><base>-<sha256[:8]>)."""
    clean_base = _SAFE_SEGMENT_RE.sub("-", base_skill.strip().lower()).strip("-") or "skill"
    if not clean_base[0].isalpha():
        clean_base = "s-" + clean_base
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()[:8]
    max_base = 64 - len(CANDIDATE_SKILL_PREFIX) - 1 - len(digest)
    name = f"{CANDIDATE_SKILL_PREFIX}{clean_base[:max_base].rstrip('-')}-{digest}"
    if not _SKILL_NAME_RE.fullmatch(name):
        raise InvalidError("invalid_candidate_name", f"无法生成合法候选 Skill 名：{name!r}")
    return name


class IsolatedEvolutionRegistry:
    """Registry wrapper guaranteeing automated evolution never mutates platform or user scopes.

    Read methods are forwarded with tenant verification; automated writes are restricted to
    staging `cand-*` skills exclusively inside the `sys-evolution` tenant scope.
    Platform promotion requires `EvolutionStore.approve_and_promote(...)` with a human admin actor.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def list_skills(self, tenant: Tenant, pins: Mapping[str, str] | None = None) -> list[rg.Skill]:
        assert_evolution_tenant(tenant)
        return await self._inner.list_skills(tenant, pins)

    async def get_skill(self, tenant: Tenant, name: str, version: str = "") -> rg.Skill:
        assert_evolution_tenant(tenant)
        return await self._inner.get_skill(tenant, name, version)

    async def list_model_profiles(self, tenant: Tenant) -> list[Any]:
        assert_evolution_tenant(tenant)
        return await self._inner.list_model_profiles(tenant)

    async def stage_candidate_skill(
        self,
        base_skill: str,
        document: Mapping[str, Any],
        *,
        change_note: str = "",
        workspace_id: str = "eval-ws",
    ) -> tuple[str, rg.PublishResponse]:
        """Stage a candidate skill as `cand-<base>-<hash>` strictly under sys-evolution tenant."""
        cand_name = candidate_skill_name(base_skill, document)
        tenant = evolution_worker_tenant(workspace_id)
        resp = await self._inner.publish(
            tenant,
            rg.REGISTRY_KIND_SKILL,
            cand_name,
            document,
            change_note=change_note or f"evolution candidate for {base_skill}",
        )
        return cand_name, resp

    async def publish(
        self,
        tenant: Tenant | None,
        kind: int,
        name: str,
        document: Mapping[str, Any],
        *,
        change_note: str = "",
    ) -> rg.PublishResponse:
        if tenant is None:
            raise DeniedError(
                "unapproved_platform_write",
                "自动化自进化禁止直接写入平台级 Registry；必须走 sys-evolution 候选暂存并由管理员审批晋升",
            )
        assert_evolution_tenant(tenant)
        if kind != rg.REGISTRY_KIND_SKILL or not name.startswith(CANDIDATE_SKILL_PREFIX):
            raise DeniedError(
                "unapproved_platform_write",
                f"sys-evolution 仅允许暂存 {CANDIDATE_SKILL_PREFIX}* 候选 Skill",
            )
        return await self._inner.publish(tenant, kind, name, document, change_note=change_note)

    async def promote(self, *args: Any, **kwargs: Any) -> Any:
        raise DeniedError(
            "unapproved_platform_write",
            "自动化自进化禁止直接调用 Registry.Promote；必须由人类管理员审批后执行",
        )
