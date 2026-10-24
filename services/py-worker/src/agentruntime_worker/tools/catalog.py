"""工具目录：工具的 effect、说明与参数 schema 取自 ToolDriver 的登记（T-10，细化见 docs/plan/tool-driver.md 第 2 节第 1、3 条）。

- 同名的工具取版本号最大的（按数字比，v10 > v9）；没登记的（含别的租户私有的）不给。
- ToolCatalog：resolve_catalog 的工具部分、tool_loop 给模型的工具定义都用它；Node Executor 的 call_tool 用 latest_spec 取同一个版本。
- DevCatalog：没接 Registry 时的 resolve_catalog：模板仍是桩（program · none），工具 effect 取自登记——不再一律是 read。
  没登记的工具不在结果里，Stage Runner 把它当作 write_irreversible。
"""

from __future__ import annotations

from collections.abc import Sequence

from temporalio import activity

from agentruntime_core.providers.tool_driver import ToolDriver, ToolDriverProvider, ToolSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker import names
from agentruntime_worker.activities.types import CatalogInfo, CatalogRequest, Tenant, TemplateInfo


def _version(spec: ToolSpec) -> int:
    return int(spec.version[1:]) if spec.version[1:].isdigit() else -1


def latest(specs: Sequence[ToolSpec]) -> dict[str, ToolSpec]:
    """名字 → 版本号最大的登记。"""
    out: dict[str, ToolSpec] = {}
    for spec in specs:
        current = out.get(spec.name)
        if current is None or _version(spec) > _version(current):
            out[spec.name] = spec
    return out


async def latest_spec(driver: ToolDriver, name: str) -> ToolSpec | None:
    """调用时用的登记：同名取最大版本；没登记返回 None。"""
    return latest(await driver.list_tools()).get(name)


class ToolCatalog:
    """按租户查工具的登记。只用租户、用户与 workspace 绑定（工具的可见范围不看 Session）。"""

    def __init__(self, provider: ToolDriverProvider) -> None:
        self.provider = provider

    async def _latest(self, tenant: Tenant) -> dict[str, ToolSpec]:
        driver = self.provider.bind(TenantContext(tenant_id=tenant.tenant_id, user_id=tenant.user_id, workspace_id=tenant.workspace_id))
        return latest(await driver.list_tools())

    async def specs(self, tenant: Tenant, names_: Sequence[str]) -> dict[str, ToolSpec]:
        found = await self._latest(tenant)
        return {n: found[n] for n in names_ if n in found}

    async def effects(self, tenant: Tenant, names_: Sequence[str]) -> dict[str, str]:
        return {n: spec.effect.value for n, spec in (await self.specs(tenant, names_)).items()}

    async def all_effects(self, tenant: Tenant) -> dict[str, str]:
        """这个租户看得到的全部工具 → effect（同名取最大版本），给 Planner 的 Node Catalog 用。"""
        return {n: spec.effect.value for n, spec in sorted((await self._latest(tenant)).items())}


class DevCatalog:
    """没接 Registry 时的 resolve_catalog：模板是桩，工具 effect 取自 ToolDriver 的登记。"""

    def __init__(self, tools: ToolCatalog) -> None:
        self.tools = tools

    @activity.defn(name=names.RESOLVE_CATALOG)
    async def resolve_catalog(self, req: CatalogRequest) -> CatalogInfo:
        tools = await self.tools.effects(req.tenant, list(req.tools)) if req.tools else {}
        return CatalogInfo(templates={t: TemplateInfo(executor="program", effect="none") for t in req.templates}, tools=tools)

    def activities(self) -> dict[str, object]:
        return {names.RESOLVE_CATALOG: self.resolve_catalog}
