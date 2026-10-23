"""T-10 验收：工具的 effect 取自 ToolDriver 的登记（关掉 T-09 登记的缺口；细化见 docs/plan/tool-driver.md 第 2 节第 1 条）。

- ToolCatalog：按租户列出工具，同名取版本号最大的；没登记的不给（Stage Runner 按 write_irreversible 处理）。
- resolve_catalog：没接 Registry 时的开发 Catalog（模板仍是桩）与 Registry Catalog，工具部分都由 ToolCatalog 给出，
  不再一律是 read。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio.testing import ActivityEnvironment

from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.enums import Effect
from agentruntime_core.providers.tool_driver import ToolInvocation, ToolSpec

from agentruntime_worker.activities.types import CatalogRequest, Tenant, TemplateInfo
from agentruntime_worker.registry import GrpcRegistry, RegistryCatalog
from agentruntime_worker.tools import DevCatalog, ToolCatalog

from .registry_fakes import serve, template
from .test_registry import seeded

T1 = Tenant(tenant_id="t1", user_id="u1", workspace_id="w1")
T2 = Tenant(tenant_id="t2", user_id="u2", workspace_id="w1")


async def _noop(call: ToolInvocation) -> Any:
    return {}


def spec(name: str, effect: Effect, version: str = "v1", description: str = "") -> ToolSpec:
    return ToolSpec(
        name=name,
        version=version,
        description=description or name,
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        output_schema={"type": "object"},
        effect=effect,
        permissions=frozenset(),
        timeout=timedelta(seconds=5),
    )


def provider() -> LocalToolDriverProvider:
    p = LocalToolDriverProvider()
    p.register(spec("web_search", Effect.READ), _noop)
    p.register(spec("card_charge", Effect.WRITE_IRREVERSIBLE), _noop)
    p.register(spec("repo_edit", Effect.WRITE_REVERSIBLE), _noop)
    # 同名的两个版本：取版本号最大的（v10 > v9，按数字比）。
    p.register(spec("crm_sync", Effect.READ, version="v9"), _noop)
    p.register(spec("crm_sync", Effect.WRITE_REVERSIBLE, version="v10", description="同步 CRM（新版会写）"), _noop)
    p.register(spec("t2_private", Effect.READ), _noop, tenant_id="t2")
    return p


async def test_effects_come_from_the_registrations() -> None:
    catalog = ToolCatalog(provider())
    effects = await catalog.effects(T1, ["web_search", "card_charge", "repo_edit", "crm_sync", "ghost", "t2_private"])
    assert effects == {
        "web_search": "read",
        "card_charge": "write_irreversible",
        "repo_edit": "write_reversible",
        "crm_sync": "write_reversible",
    }, "同名取最大版本；没登记的（含别的租户私有的）不给"


async def test_specs_carry_descriptions_and_schemas() -> None:
    catalog = ToolCatalog(provider())
    specs = await catalog.specs(T1, ["crm_sync", "ghost"])
    assert list(specs) == ["crm_sync"]
    assert specs["crm_sync"].version == "v10" and specs["crm_sync"].description == "同步 CRM（新版会写）"
    assert specs["crm_sync"].input_schema["properties"]["q"] == {"type": "string"}


async def test_tenant_private_tools_are_seen_by_their_tenant() -> None:
    catalog = ToolCatalog(provider())
    assert await catalog.effects(T2, ["t2_private"]) == {"t2_private": "read"}


def request(tools: list[str], templates: list[str] | None = None) -> CatalogRequest:
    return CatalogRequest(tenant=T1, task_id="tsk_cat", templates=templates or [], tools=tools, pinned={})


async def test_the_dev_catalog_reports_real_tool_effects() -> None:
    catalog = DevCatalog(ToolCatalog(provider()))
    info = await ActivityEnvironment().run(catalog.resolve_catalog, request(["card_charge", "web_search", "ghost"], ["step@v1"]))
    assert info.tools == {"card_charge": "write_irreversible", "web_search": "read"}, "不再一律 read；没登记的不给"
    assert info.templates == {"step@v1": TemplateInfo(executor="program", effect="none")}, "没接 Registry 时模板仍是桩"
    assert not info.authoritative


async def test_the_registry_catalog_takes_tool_effects_from_the_driver() -> None:
    fake = seeded()
    fake.add(template("rubric_judge", "v1", "model", "none", "score"))
    async with serve(fake) as address:
        catalog = RegistryCatalog(GrpcRegistry(address), tools=ToolCatalog(provider()))
        info = await ActivityEnvironment().run(catalog.resolve_catalog, request(["card_charge", "ghost"], ["rubric_judge@v1"]))
    assert info.tools == {"card_charge": "write_irreversible"}
    assert fake.calls("GetNodeTemplate"), "模板仍然问 Registry"
