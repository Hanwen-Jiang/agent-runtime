"""用户 Skill 的 Worker 一侧（N-14，docs/plan/n14-skill-contract.md；契约 c1836ba）。

- TaskSkillPins：Task 固定的 Skill（名字 → <层>/vN）与提交者，读业务库 tasks 表的 skill_pins、submitted_by（task-service 提交时写，
  这里只读）。按 task_id 读，不经工作流，所以不改命令序列。没有这行（sub_task 的子 Task 只在工作流里）、旧库没有这一列、读失败时
  当作没有 Skill。
- SkillsContextSource：Planner 的上下文加上 Task 固定的 Skill 的名字与说明（catalog.skills），经 Registry 的 ListSkills 按固定的
  版本取（不带正文）；Registry 还不认这个方法（unimplemented）时没有 Skill，别的错误照常抛（Temporal 重试）。
- skill_read（effect read）：按 Task 固定的版本读一个 Skill 的 instructions（加文件清单），或它的一份文件。没固定的名字是
  skill_not_pinned；用户层按提交者读（Registry 的 TenantContext.user_id 是 submitted_by，不是调用里的用户）。
  正文只进这次工具调用的结果（之后照 Context Builder 的规则转存），不进日志。
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Protocol

from google.protobuf import json_format

from agentruntime.registry.v1 import registry_pb2 as rg
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.enums import Effect
from agentruntime_core.errors import DeniedError, InvalidError, ProviderError
from agentruntime_core.providers.relational_store import RelationalStoreProvider
from agentruntime_core.providers.tool_driver import ToolInvocation, ToolSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.activities.types import PlanRoundInput, Tenant
from agentruntime_worker.planner.context import PlanningContext, PlanningContextSource

logger = logging.getLogger(__name__)

SKILL_READ = "skill_read"
VERSION = "v1"


class SkillRegistry(Protocol):
    async def list_skills(self, tenant: Tenant, pins: Mapping[str, str] | None) -> list[rg.Skill]: ...

    async def get_skill(self, tenant: Tenant, name: str, version: str = "") -> rg.Skill: ...


@dataclasses.dataclass(frozen=True)
class SkillPins:
    pins: dict[str, str]
    submitter: str = ""


class TaskSkillPins:
    """读 tasks.skill_pins 与 submitted_by；store 是 RelationalStoreProvider（或 LazyRelationalStore）。"""

    def __init__(self, store: RelationalStoreProvider) -> None:
        self.store = store

    async def load(self, tenant_id: str, task_id: str) -> SkillPins:
        if not tenant_id or not task_id:
            return SkillPins({})
        try:
            # 行按租户隔离（RelationalStore 负责），task_id 是查询条件。
            async with self.store.bind(TenantContext(tenant_id=tenant_id)).transaction(read_only=True) as tx:
                row = await tx.query_one("SELECT skill_pins, submitted_by FROM tasks WHERE task_id = $1", (task_id,))
        except Exception as exc:  # noqa: BLE001 — 旧库没有这一列、读不到：当作没有 Skill
            logger.warning("读 Task 固定的 Skill 失败，当作没有：%s", type(exc).__name__)
            return SkillPins({})
        if row is None or not row["skill_pins"]:
            return SkillPins({})
        try:
            pins = json.loads(row["skill_pins"])
        except ValueError:
            return SkillPins({})
        if not isinstance(pins, dict):
            return SkillPins({})
        return SkillPins({str(k): str(v) for k, v in pins.items()}, str(row["submitted_by"] or ""))


def _owner_tenant(tenant: Tenant, submitter: str) -> Tenant:
    """读用户层要用提交者：Registry 按 TenantContext.user_id 找用户层。"""
    return dataclasses.replace(tenant, user_id=submitter) if submitter else tenant


async def skill_catalog(registry: SkillRegistry, pins: TaskSkillPins, inp: PlanRoundInput) -> dict[str, str]:
    """Task 固定的 Skill 的名字 → 说明；没有固定的、Registry 还不认 ListSkills 时为空。"""
    pinned = await pins.load(inp.tenant.tenant_id, inp.task_id)
    if not pinned.pins:
        return {}
    try:
        listed = await registry.list_skills(_owner_tenant(inp.tenant, pinned.submitter), pinned.pins)
    except ProviderError as err:
        if err.code == "unimplemented":
            return {}
        raise
    return {s.name: s.description for s in listed}


class SkillsContextSource:
    """在 inner 给出的规划上下文上加上 Task 固定的 Skill 的名字与说明（RegistryContextSource 给了 skill_pins 时自己加，不用它）。"""

    def __init__(self, inner: PlanningContextSource, registry: SkillRegistry, pins: TaskSkillPins) -> None:
        self.inner = inner
        self.registry = registry
        self.pins = pins

    async def load(self, inp: PlanRoundInput) -> PlanningContext:
        ctx = await self.inner.load(inp)
        skills = await skill_catalog(self.registry, self.pins, inp)
        if not skills:
            return ctx
        return dataclasses.replace(ctx, catalog=dataclasses.replace(ctx.catalog, skills=skills))


SPEC = ToolSpec(
    name=SKILL_READ,
    version=VERSION,
    description="读这个 Task 可用的一个 Skill（规划时列出的名字）：不给 path 时返回做法说明（instructions）与它带的文件清单；给 path 时返回那份文件",
    input_schema={
        "type": "object",
        "required": ["name"],
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 64, "description": "Skill 的名字"},
            "path": {"type": "string", "minLength": 1, "maxLength": 255, "description": "Skill 里的一份文件（文件清单里的 path）"},
        },
    },
    output_schema={"type": "object"},
    effect=Effect.READ,
    permissions=frozenset(),
    timeout=timedelta(seconds=30),
)


class SkillRead:
    """skill_read 的处理函数。"""

    def __init__(self, registry: SkillRegistry, pins: TaskSkillPins) -> None:
        self.registry = registry
        self.pins = pins

    async def __call__(self, invocation: ToolInvocation) -> Any:
        if not invocation.tenant_id:
            raise DeniedError("no_tenant", "skill_read 只在租户作用域里可用")
        name = str(invocation.arguments.get("name") or "")
        pinned = await self.pins.load(invocation.tenant_id, invocation.task_id)
        version = pinned.pins.get(name)
        if version is None:
            available = "、".join(sorted(pinned.pins)) or "无"
            raise InvalidError("skill_not_pinned", f"这个 Task 没有名为 {name} 的 Skill；可用的：{available}")
        tenant = Tenant(tenant_id=invocation.tenant_id, user_id=pinned.submitter or invocation.user_id, workspace_id=invocation.workspace_id,
                        session_id=invocation.session_id)
        found = await self.registry.get_skill(tenant, name, version)
        doc = json_format.MessageToDict(found.document.value) if found.document.HasField("value") else {}
        files = [f for f in doc.get("files") or [] if isinstance(f, dict)]
        path = invocation.arguments.get("path")
        if path is None:
            return {
                "name": name, "version": version, "description": str(doc.get("description") or ""), "instructions": str(doc.get("instructions") or ""),
                "files": [{"path": str(f.get("path")), "media_type": str(f.get("media_type") or "text/plain")} for f in files],
            }
        for f in files:
            if f.get("path") == path:
                return {"name": name, "version": version, "path": str(path), "media_type": str(f.get("media_type") or "text/plain"), "content": str(f.get("content") or "")}
        listed = "、".join(str(f.get("path")) for f in files) or "无"
        raise InvalidError("skill_file_not_found", f"Skill {name} 里没有文件 {path}；有这些文件：{listed}")


def register_skill_tools(provider: LocalToolDriverProvider, *, registry: SkillRegistry, pins: TaskSkillPins) -> None:
    provider.register(SPEC, SkillRead(registry, pins))


__all__ = ["SKILL_READ", "SPEC", "SkillPins", "SkillRead", "SkillsContextSource", "TaskSkillPins", "register_skill_tools", "skill_catalog"]
