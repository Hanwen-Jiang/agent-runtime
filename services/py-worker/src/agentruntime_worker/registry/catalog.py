"""Registry 版的 resolve_catalog 与 Planner 上下文（T-08，细化见 docs/plan/registry.md 第 8 节第 7、8 条）。

- RegistryCatalog：Stage Runner 查模板（Atomic Node 与 quality 的 judge、scorer）的执行方式。Task 有固定版本时，引用的 name@vN 必须与固定的一致；
  没有固定版本（Registry 接入之前启动的 Task）时按引用里的版本取。模板不存在、引用没固定都是不可重试的错误；
  Registry 不可用时抛 TransientError，由 Temporal 重试，不回退到“program · none”的桩。结果标为 authoritative，Stage Runner
  据此核对 Atomic Node 的 executor、effect（T-09）。工具的 effect 不归 Registry，
  由 tools（工具目录 ToolCatalog，取自 ToolDriver 的登记，T-10）给出；没登记的工具不在结果里。
- RegistryStagePolicy（T-09）：load_stage_policy 把 Task 固定的租户策略与 Recipe 声明的 hooks 合并进 StagePolicy。
- RegistryContextSource：Planner 的 Node Catalog 按 Task 固定的版本取自 Registry，Recipe 取自 Registry 的默认版本；
  effect 上限取 Task State 的 constraints.effect_ceiling（task-service 按租户策略写入），没有时用 defaults 的；
  Recipe 的 recent_failure 第一期都是 false（Recipe 统计在 T-16）。工具与 risk_authorized 取 defaults。
  可用的 Tier 取自默认版本的 Model Profile：能生成（capabilities 含 generate）的那些的 <class>@<tier 名>；嵌入模型不列，
  嵌入节点不写 tier，Model Gateway 按调用种类取默认。
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable

from google.protobuf import json_format
from temporalio import activity
from temporalio.exceptions import ApplicationError

from agentruntime.common.v1 import enums_pb2
from agentruntime.registry.v1 import registry_pb2 as rg
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError

from agentruntime_worker import names
from agentruntime_worker.activities.types import CatalogInfo, CatalogRequest, PlanRoundInput, StagePolicy, StagePolicyRequest, TemplateInfo
from agentruntime_worker.planner.context import STUB_TEMPLATE_PREFIX, CatalogView, PlanningContext, RecipeEntry
from agentruntime_worker.registry.client import GrpcRegistry
from agentruntime_worker.tools.catalog import ToolCatalog
from agentruntime_worker.tools.skills import TaskSkillPins, skill_catalog

_REF = re.compile(r"^([a-z][a-z0-9_]*)@(v[0-9]+)$")


def _enum(value: int, enum: object, prefix: str) -> str:
    """proto 枚举值 → 附录 A 的取值（EXECUTOR_MODEL → model）；UNSPECIFIED 为空串。"""
    name = enum.Name(value)  # type: ignore[attr-defined]
    return "" if name.endswith("_UNSPECIFIED") else name.removeprefix(prefix).lower()


def template_info(t: rg.NodeTemplate) -> TemplateInfo:
    return TemplateInfo(
        executor=_enum(t.executor, enums_pb2.Executor, "EXECUTOR_"),
        effect=_enum(t.effect, enums_pb2.Effect, "EFFECT_"),
        output=_enum(t.output, enums_pb2.ModelOutput, "MODEL_OUTPUT_"),
    )


class RegistryCatalog:
    def __init__(self, registry: GrpcRegistry, *, tools: ToolCatalog) -> None:
        self.registry = registry
        self.tools = tools

    @activity.defn(name=names.RESOLVE_CATALOG)
    async def resolve_catalog(self, req: CatalogRequest) -> CatalogInfo:
        pinned = dict((req.pinned or {}).get("node_templates") or {})
        refs: list[tuple[str, str, str]] = []
        for ref in req.templates:
            m = _REF.match(ref)
            if m is None:
                raise ApplicationError(f"模板引用 {ref!r} 要写成 name@vN", type="invalid_template_ref", non_retryable=True)
            name, version = m.group(1), m.group(2)
            if pinned and pinned.get(name) != version:
                raise ApplicationError(
                    f"模板 {ref} 不是 Task 固定的版本（固定的是 {pinned.get(name) or '无'}）", type="template_not_pinned", non_retryable=True
                )
            refs.append((ref, name, version))
        templates: dict[str, TemplateInfo] = {}
        for ref, name, version in refs:
            try:
                info = template_info(await self.registry.get_node_template(req.tenant, name, version))
            except NotFoundError:
                raise ApplicationError(f"Registry 里没有模板 {ref}", type="template_not_found", non_retryable=True) from None
            except (DeniedError, InvalidError) as err:
                raise ApplicationError(f"查模板 {ref} 被拒：{err.code}", type="registry_rejected", non_retryable=True) from None
            if not info.executor or not info.effect:
                raise ApplicationError(f"Registry 给的模板 {ref} 缺 executor 或 effect", type="invalid_template", non_retryable=True)
            templates[ref] = info
        tools = await self.tools.effects(req.tenant, list(req.tools)) if req.tools else {}
        return CatalogInfo(templates=templates, tools=tools, authoritative=True)

    def activities(self) -> dict[str, object]:
        return {names.RESOLVE_CATALOG: self.resolve_catalog}


class RegistryContextSource:
    def __init__(self, registry: GrpcRegistry, *, defaults: PlanningContext, skill_pins: TaskSkillPins | None = None) -> None:
        self.registry = registry
        self.defaults = defaults
        # N-14：给了就把 Task 固定的 Skill 的名字与说明放进 catalog.skills（固定的版本在业务库的 tasks.skill_pins）。
        self.skill_pins = skill_pins

    async def load(self, inp: PlanRoundInput) -> PlanningContext:
        templates = await self.registry.list_node_templates(inp.tenant, inp.pinned)
        recipes = await self.registry.list_recipes(inp.tenant)
        profiles = await self.registry.list_model_profiles(inp.tenant)
        catalog = CatalogView(
            templates={
                f"{t.name}@{t.version}": (info.executor, info.effect)
                for t in templates
                if not t.name.startswith(STUB_TEMPLATE_PREFIX)
                for info in [template_info(t)]
            },
            recipes={f"{r.name}@{r.version}": r.task_family for r in recipes},
            tools=dict(self.defaults.catalog.tools),
            tiers=generation_tiers(_document(p.document) for p in profiles),
            skills=await skill_catalog(self.registry, self.skill_pins, inp) if self.skill_pins is not None else {},
        )
        entries = []
        for r in recipes:
            doc = json_format.MessageToDict(r.document.value) if r.document.HasField("value") else {}
            cost = doc.get("typical_cost_usd")
            entries.append(
                RecipeEntry(
                    recipe=f"{r.name}@{r.version}",
                    task_family=r.task_family,
                    risk=str(doc.get("risk") or "read"),
                    typical_cost_usd=float(cost) if cost is not None else None,
                )
            )
        ceiling = (inp.task_state.get("constraints") or {}).get("effect_ceiling") or self.defaults.effect_ceiling
        # TMP 计划第 6 节：租户的任务族词表；还没有时为 None，照旧规划。
        vocabulary = await self.registry.get_task_families(inp.tenant)
        families = vocabulary.families if vocabulary is not None else None
        return dataclasses.replace(self.defaults, catalog=catalog, recipes=entries, effect_ceiling=str(ceiling), task_families=families)


class RegistryStagePolicy:
    """load_stage_policy：Task 固定的租户策略版本与固定的 Recipe 的 hooks 合并进 StagePolicy（T-09，细化第 2 节第 3 条）。

    按“租户策略、Recipe”的顺序合并，同一挂载点上同名的只留第一个；其余上限取 defaults。没有固定的就不问 Registry。
    固定的版本取不到（NotFound）或被拒时不可重试地失败：租户策略与安全类 Hook 取不到一律按拒绝处理（设计书 3.1.2）；
    Registry 不可用时抛 TransientError，由 Temporal 重试。
    """

    def __init__(self, registry: GrpcRegistry, *, defaults: StagePolicy) -> None:
        self.registry = registry
        self.defaults = defaults

    @activity.defn(name=names.LOAD_STAGE_POLICY)
    async def load_stage_policy(self, req: StagePolicyRequest) -> StagePolicy:
        pinned = req.pinned or {}
        hooks: dict[str, list[str]] = {}
        version = str(pinned.get("tenant_policy") or "")
        if version:
            try:
                policy = await self.registry.get_tenant_policy(req.tenant, version)
            except NotFoundError:
                raise ApplicationError(f"Task 固定的租户策略 {version} 取不到", type="tenant_policy_not_found", non_retryable=True) from None
            except (DeniedError, InvalidError) as err:
                raise ApplicationError(f"取租户策略被拒：{err.code}", type="registry_rejected", non_retryable=True) from None
            _merge(hooks, _document(policy.document).get("hooks"))
        recipe = str(pinned.get("recipe") or "")
        if recipe:
            m = _REF.match(recipe)
            if m is None:
                raise ApplicationError(f"固定的 Recipe {recipe!r} 不是 name@vN", type="invalid_recipe_ref", non_retryable=True)
            try:
                found = await self.registry.get_recipe(req.tenant, m.group(1), m.group(2))
            except NotFoundError:
                raise ApplicationError(f"Task 固定的 Recipe {recipe} 取不到", type="recipe_not_found", non_retryable=True) from None
            except (DeniedError, InvalidError) as err:
                raise ApplicationError(f"取 Recipe 被拒：{err.code}", type="registry_rejected", non_retryable=True) from None
            _merge(hooks, _document(found.document).get("hooks"))
        return dataclasses.replace(self.defaults, hooks=hooks)

    def activities(self) -> dict[str, object]:
        return {names.LOAD_STAGE_POLICY: self.load_stage_policy}


def generation_tiers(documents: Iterable[dict]) -> tuple[str, ...]:
    """Model Profile 的正文 → 能生成的 Tier（<class>@<tier 名>），排序去重。"""
    found: set[str] = set()
    for doc in documents:
        if "generate" not in (doc.get("capabilities") or []) or not doc.get("class"):
            continue
        found |= {f"{doc['class']}@{t['name']}" for t in doc.get("tiers") or [] if isinstance(t, dict) and t.get("name")}
    return tuple(sorted(found))


def _document(payload: object) -> dict:
    value = getattr(payload, "value", None)
    if value is None or not payload.HasField("value"):  # type: ignore[attr-defined]
        return {}
    doc = json_format.MessageToDict(value)
    return doc if isinstance(doc, dict) else {}


def _merge(into: dict[str, list[str]], declared: object) -> None:
    """hook_set（挂载点 → Hook 名）并进 into，同一挂载点上同名的只留第一个。"""
    if not isinstance(declared, dict):
        return
    for mount, names_ in declared.items():
        target = into.setdefault(str(mount), [])
        for name in names_ if isinstance(names_, list) else []:
            if name not in target:
                target.append(str(name))
