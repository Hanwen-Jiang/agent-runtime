"""Registry 版的 resolve_catalog 与 Planner 上下文（T-08，细化见 docs/plan/registry.md 第 8 节第 7、8 条）。

- RegistryCatalog：Stage Runner 查 judge、scorer 模板的执行方式。Task 有固定版本时，引用的 name@vN 必须与固定的一致；
  没有固定版本（Registry 接入之前启动的 Task）时按引用里的版本取。模板不存在、引用没固定都是不可重试的错误；
  Registry 不可用时抛 TransientError，由 Temporal 重试，不回退到“program · none”的桩。工具的 effect 不归 Registry，
  由 tool_effects 给出（工具目录；T-10 接 ToolDriver）。
- RegistryContextSource：Planner 的 Node Catalog 按 Task 固定的版本取自 Registry，Recipe 取自 Registry 的默认版本；
  effect 上限取 Task State 的 constraints.effect_ceiling（task-service 按租户策略写入），没有时用 defaults 的；
  Recipe 的 recent_failure 第一期都是 false（Recipe 统计在 T-16）。工具与 risk_authorized 取 defaults。
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable

from google.protobuf import json_format
from temporalio import activity
from temporalio.exceptions import ApplicationError

from agentruntime.common.v1 import enums_pb2
from agentruntime.registry.v1 import registry_pb2 as rg
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError

from agentruntime_worker import names
from agentruntime_worker.activities.types import CatalogInfo, CatalogRequest, PlanRoundInput, TemplateInfo
from agentruntime_worker.planner.context import CatalogView, PlanningContext, RecipeEntry
from agentruntime_worker.registry.client import GrpcRegistry

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
    def __init__(self, registry: GrpcRegistry, *, tool_effects: Callable[[list[str]], dict[str, str]]) -> None:
        self.registry = registry
        self.tool_effects = tool_effects

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
        tools = self.tool_effects(list(req.tools)) if req.tools else {}
        return CatalogInfo(templates=templates, tools=tools)

    def activities(self) -> dict[str, object]:
        return {names.RESOLVE_CATALOG: self.resolve_catalog}


class RegistryContextSource:
    def __init__(self, registry: GrpcRegistry, *, defaults: PlanningContext) -> None:
        self.registry = registry
        self.defaults = defaults

    async def load(self, inp: PlanRoundInput) -> PlanningContext:
        templates = await self.registry.list_node_templates(inp.tenant, inp.pinned)
        recipes = await self.registry.list_recipes(inp.tenant)
        catalog = CatalogView(
            templates={f"{t.name}@{t.version}": (info.executor, info.effect) for t in templates for info in [template_info(t)]},
            recipes={f"{r.name}@{r.version}": r.task_family for r in recipes},
            tools=dict(self.defaults.catalog.tools),
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
        return dataclasses.replace(self.defaults, catalog=catalog, recipes=entries, effect_ceiling=str(ceiling))
