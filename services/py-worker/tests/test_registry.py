"""T-08 Registry 的 Worker 一侧（docs/plan/registry.md 第 2 节第 6 条、第 4 节、第 5 节“Worker”）；文件末尾是 T-09 的 Hook 来源。

假的 RegistryService 在进程内（grpc.aio，见 registry_fakes）。覆盖：
- 客户端 GrpcRegistry：每个请求带租户；列表把分页取完；按固定的版本列模板；错误按 trailer ar-error-bin 还原；
  Publish、Promote 带发布令牌（authorization: Bearer …），读方法不带；令牌不出现在 repr 与错误里；from_env。
- resolve_catalog（Stage Runner 查 judge、scorer 模板的执行方式）：按 Task 固定的版本查 Registry；引用的版本与固定的不一致、
  模板不存在都是不可重试的错误；Registry 不可用时 activity 可重试地失败，不回退到“program · none”的桩
  （那等于把写操作当成无副作用执行）；工具的 effect 仍由工具目录给出（ToolDriver，T-10），不问 Registry。
- Planner 的上下文：Node Catalog 按固定的版本取自 Registry，Recipe 取自 Registry 的默认版本；effect 上限取 Task 的约束；
  Registry 不可用时 plan_round 可重试地失败，不用写死的上下文。
- 组装：设了 AR_REGISTRY_ADDRESS 才换上这些；没设时仍是桩（端到端测试的默认不变）。
"""

from __future__ import annotations

import dataclasses

import grpc
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from agentruntime.common.v1 import tenant_pb2
from agentruntime.registry.v1 import registry_pb2 as rg
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError, TransientError

from agentruntime_worker import names
from agentruntime_worker.activities.types import CatalogRequest, PlanRoundInput, StagePolicy, StagePolicyRequest, Tenant, TemplateInfo
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.planner.context import CatalogView, PlanningContext, RecipeEntry
from agentruntime_worker.registry import GrpcRegistry, MissingRegistryAddressError, RegistryCatalog, RegistryContextSource, RegistryStagePolicy, from_env
from agentruntime_worker.worker import decision_activities

from .gateway_fakes import Abort
from .registry_fakes import FakeRegistry, model_profile, not_found, recipe, serve, template, unavailable

TENANT = Tenant(tenant_id="t-reg", user_id="u-reg", workspace_id="w-reg", session_id="ses-reg")
TOKEN = "worker-test-publish-token-5d2f"
PINNED = {
    "recipe": "refund_analysis@v1",
    "node_templates": {"answer": "v1", "rubric_judge": "v1", "sql_query": "v1"},
    "model_profiles": {"a6api/gpt-4o-mini@2026-10": "v1"},
}


def tenant_proto() -> tenant_pb2.TenantContext:
    return tenant_pb2.TenantContext(tenant_id="t-reg", user_id="u-reg", workspace_id="w-reg", session_id="ses-reg")


def seeded() -> FakeRegistry:
    fake = FakeRegistry()
    fake.add(
        template("answer", "v1", "model", "none", "generate"),
        template("rubric_judge", "v1", "model", "none", "score"),
        template("rubric_judge", "v2", "model", "none", "score", state=rg.RELEASE_STATE_SHADOW),
        template("sql_query", "v1", "program", "read"),
        template("ledger_write", "v1", "program", "write_reversible"),
        template("alpha", "v1", "model", "none", "generate"),
    )
    fake.recipes = [
        recipe("refund_analysis", "v1", "analytics.attribution", "read", 0.4),
        recipe("ticket_triage", "v4", "support.ticket_batch", "write_reversible"),
        recipe("weekly", "v2", "research.weekly", "none"),
    ]
    fake.model_profiles = [
        model_profile("a6api/gpt-4o-mini@2026-10", "small", ["low"]),
        model_profile("a6api/deepseek-v4-flash@2026-10", "mid", ["low"]),
        model_profile("a6api/claude-sonnet-5-5@2026-10", "strong", ["medium", "high"]),
        model_profile("a6api/gpt-5.5@2026-10", "strong", ["medium", "high"]),
        model_profile("a6api/text-embedding-3-small@2026-10", "embed", ["default"], capabilities=["embed"]),
    ]
    return fake


# --- 客户端 ---


async def test_requests_carry_the_tenant() -> None:
    fake = seeded()
    async with serve(fake) as address:
        client = GrpcRegistry(address)
        try:
            got = await client.get_node_template(TENANT, "answer", "v1")
        finally:
            await client.aclose()
    assert got.name == "answer" and got.version == "v1"
    (request,) = fake.calls("GetNodeTemplate")
    assert request.tenant == tenant_proto()
    assert (request.name, request.version) == ("answer", "v1")


async def test_listing_templates_sends_the_pins_and_reads_every_page() -> None:
    fake = seeded()
    async with serve(fake) as address:
        client = GrpcRegistry(address)
        try:
            pinned = await client.list_node_templates(TENANT, PINNED)
            current = await client.list_node_templates(TENANT, None)
        finally:
            await client.aclose()
    assert [(t.name, t.version) for t in pinned] == [("answer", "v1"), ("rubric_judge", "v1"), ("sql_query", "v1")]
    assert len(current) == 5, "不带固定版本时是默认版本，分页（每页 2 条）取完"
    first = fake.calls("ListNodeTemplates")[0]
    assert dict(first.pinned_versions.node_templates) == PINNED["node_templates"]
    assert first.pinned_versions.recipe == "refund_analysis@v1"
    assert dict(first.pinned_versions.model_profiles) == PINNED["model_profiles"]
    assert all(r.tenant == tenant_proto() for r in fake.calls("ListNodeTemplates"))


async def test_listing_recipes_reads_every_page() -> None:
    fake = seeded()
    async with serve(fake) as address:
        client = GrpcRegistry(address)
        try:
            recipes = await client.list_recipes(TENANT)
            family = await client.list_recipes(TENANT, "analytics.attribution")
        finally:
            await client.aclose()
    assert [(r.name, r.version) for r in recipes] == [("refund_analysis", "v1"), ("ticket_triage", "v4"), ("weekly", "v2")]
    assert [r.name for r in family] == ["refund_analysis"]
    assert fake.calls("ListRecipes")[-1].task_family == "analytics.attribution"


@pytest.mark.parametrize(
    ("abort", "expected"),
    [
        (not_found("template_not_found"), NotFoundError),
        (unavailable(), TransientError),
        (Abort(grpc.StatusCode.PERMISSION_DENIED, "denied"), DeniedError),
        (Abort(grpc.StatusCode.INVALID_ARGUMENT, "bad"), InvalidError),
    ],
)
async def test_errors_are_restored_as_provider_errors(abort: Abort, expected: type) -> None:
    fake = seeded()
    fake.abort = abort
    async with serve(fake) as address:
        client = GrpcRegistry(address)
        try:
            with pytest.raises(expected):
                await client.get_node_template(TENANT, "answer", "v1")
        finally:
            await client.aclose()


async def test_publish_and_promote_send_the_token_and_reads_do_not() -> None:
    fake = seeded()
    document = {"description": "直接作答", "executor": "model", "effect": "none", "role": "deliver", "output": "generate"}
    evaluation = {"eval_set": {"name": "task-loop", "version": "c0117be"}, "samples": 26, "passed": True}
    async with serve(fake) as address:
        client = GrpcRegistry(address, publish_token=TOKEN)
        try:
            published = await client.publish(None, rg.REGISTRY_KIND_NODE_TEMPLATE, "answer", document, change_note="评测通过")
            await client.promote(None, rg.REGISTRY_KIND_NODE_TEMPLATE, "answer", published.version, evaluation)
            await client.get_node_template(TENANT, "answer", "v1")
        finally:
            await client.aclose()
    assert fake.metadata("Publish")[0].get("authorization") == f"Bearer {TOKEN}"
    assert fake.metadata("Promote")[0].get("authorization") == f"Bearer {TOKEN}"
    assert "authorization" not in fake.metadata("GetNodeTemplate")[0], "读方法不带发布令牌"
    (publish,) = fake.calls("Publish")
    assert not publish.HasField("tenant"), "平台作用域的发布不带租户"
    assert publish.kind == rg.REGISTRY_KIND_NODE_TEMPLATE and publish.name == "answer" and publish.change_note == "评测通过"
    assert publish.document.value.struct_value["executor"] == "model"
    (promote,) = fake.calls("Promote")
    assert promote.version == "v1" and promote.evaluation.value.struct_value["passed"] is True


async def test_the_publish_token_stays_out_of_repr_and_errors() -> None:
    fake = seeded()
    fake.abort = Abort(grpc.StatusCode.PERMISSION_DENIED, "发布令牌不对")
    async with serve(fake) as address:
        client = GrpcRegistry(address, publish_token=TOKEN)
        try:
            assert TOKEN not in repr(client)
            with pytest.raises(DeniedError) as caught:
                await client.publish(None, rg.REGISTRY_KIND_NODE_TEMPLATE, "answer", {"executor": "model"})
        finally:
            await client.aclose()
    assert TOKEN not in str(caught.value)


def test_from_env() -> None:
    with pytest.raises(MissingRegistryAddressError) as caught:
        from_env({})
    assert isinstance(caught.value, InvalidError) and isinstance(caught.value, ValueError)
    assert "AR_REGISTRY_ADDRESS" in str(caught.value)
    client = from_env({"AR_REGISTRY_ADDRESS": " 127.0.0.1:8092 "})
    assert client.address == "127.0.0.1:8092"


# --- resolve_catalog ---


def catalog_request(templates: list[str], tools: list[str] | None = None, pinned: dict | None = None) -> CatalogRequest:
    return CatalogRequest(tenant=TENANT, task_id="tsk_reg", templates=templates, tools=tools or [], pinned=PINNED if pinned is None else pinned)


class FakeTools:
    """工具目录的替身（T-10 起 RegistryCatalog 按租户异步查 ToolCatalog）：记下问了什么，按 effects 回答。"""

    def __init__(self, effects: dict[str, str] | None = None) -> None:
        self._effects = dict(effects or {})
        self.asked: list[tuple[str, list[str]]] = []

    async def effects(self, tenant: Tenant, names: list[str], task_id: str = "") -> dict[str, str]:
        self.asked.append((tenant.tenant_id, list(names)))
        return {n: self._effects[n] for n in names if n in self._effects}


def no_tools() -> FakeTools:
    return FakeTools()


async def test_resolve_catalog_reads_templates_at_their_pinned_versions() -> None:
    fake = seeded()
    async with serve(fake) as address:
        catalog = RegistryCatalog(GrpcRegistry(address), tools=no_tools())
        info = await ActivityEnvironment().run(catalog.resolve_catalog, catalog_request(["rubric_judge@v1", "sql_query@v1"]))
    assert info.templates == {
        "rubric_judge@v1": TemplateInfo(executor="model", effect="none", output="score"),
        "sql_query@v1": TemplateInfo(executor="program", effect="read", output=""),
    }
    asked = {(r.name, r.version) for r in fake.calls("GetNodeTemplate")}
    assert asked == {("rubric_judge", "v1"), ("sql_query", "v1")}
    assert all(r.tenant == tenant_proto() for r in fake.calls("GetNodeTemplate"))


async def test_resolve_catalog_rejects_a_version_the_task_did_not_pin() -> None:
    fake = seeded()
    async with serve(fake) as address:
        catalog = RegistryCatalog(GrpcRegistry(address), tools=no_tools())
        with pytest.raises(ApplicationError) as caught:
            await ActivityEnvironment().run(catalog.resolve_catalog, catalog_request(["rubric_judge@v2"]))
    assert caught.value.non_retryable and caught.value.type == "template_not_pinned"
    assert fake.calls("GetNodeTemplate") == [], "不按固定版本的引用不去 Registry 取"


async def test_resolve_catalog_without_pins_reads_the_referenced_version() -> None:
    # Registry 接入之前启动的 Task 没有固定版本：按引用里的版本取。
    fake = seeded()
    async with serve(fake) as address:
        catalog = RegistryCatalog(GrpcRegistry(address), tools=no_tools())
        info = await ActivityEnvironment().run(catalog.resolve_catalog, catalog_request(["rubric_judge@v2"], pinned={}))
    assert info.templates["rubric_judge@v2"].executor == "model"
    assert [(r.name, r.version) for r in fake.calls("GetNodeTemplate")] == [("rubric_judge", "v2")]


async def test_an_unknown_template_is_not_retryable() -> None:
    fake = seeded()
    async with serve(fake) as address:
        catalog = RegistryCatalog(GrpcRegistry(address), tools=no_tools())
        with pytest.raises(ApplicationError) as caught:
            await ActivityEnvironment().run(catalog.resolve_catalog, catalog_request(["ghost@v1"], pinned={}))
    assert caught.value.non_retryable and caught.value.type == "template_not_found"


async def test_an_unavailable_registry_fails_retryably_and_never_falls_back_to_the_stub() -> None:
    fake = seeded()
    fake.abort = unavailable()
    async with serve(fake) as address:
        catalog = RegistryCatalog(GrpcRegistry(address), tools=no_tools())
        with pytest.raises(Exception) as caught:
            await ActivityEnvironment().run(catalog.resolve_catalog, catalog_request(["sql_query@v1"]))
    err = caught.value
    retryable = isinstance(err, TransientError) or (isinstance(err, ApplicationError) and not err.non_retryable)
    assert retryable, f"Registry 不可用是临时错误，由 Temporal 重试：{err!r}"


async def test_tool_effects_still_come_from_the_tool_catalog() -> None:
    fake = seeded()
    tools = FakeTools({"send_email": "write_irreversible", "web_search": "read"})
    async with serve(fake) as address:
        catalog = RegistryCatalog(GrpcRegistry(address), tools=tools)
        info = await ActivityEnvironment().run(catalog.resolve_catalog, catalog_request([], tools=["send_email", "web_search"]))
    assert info.tools == {"send_email": "write_irreversible", "web_search": "read"}
    assert tools.asked == [("t-reg", ["send_email", "web_search"])], "按请求的租户问工具目录"
    assert fake.requests == [], "工具不问 Registry"


# --- Planner 的上下文 ---


def plan_input(task_state: dict | None = None) -> PlanRoundInput:
    return PlanRoundInput(
        tenant=TENANT,
        task_id="tsk_reg",
        round_seq=1,
        task_state=task_state or {},
        message="上季度退款率为什么上升？",
        inputs={},
        attachments=[],
        pinned=PINNED,
    )


DEFAULTS = PlanningContext(risk_authorized="read", effect_ceiling="read", catalog=CatalogView(tools={"web_search": "read"}))


async def test_the_planning_context_comes_from_the_registry() -> None:
    fake = seeded()
    async with serve(fake) as address:
        source = RegistryContextSource(GrpcRegistry(address), defaults=DEFAULTS)
        context = await source.load(plan_input())
    assert context.catalog.templates == {
        "answer@v1": ("model", "none"),
        "rubric_judge@v1": ("model", "none"),
        "sql_query@v1": ("program", "read"),
    }, "Node Catalog 按 Task 固定的版本取"
    assert context.catalog.recipes == {
        "refund_analysis@v1": "analytics.attribution",
        "ticket_triage@v4": "support.ticket_batch",
        "weekly@v2": "research.weekly",
    }
    assert RecipeEntry(recipe="refund_analysis@v1", task_family="analytics.attribution", risk="read", typical_cost_usd=0.4) in context.recipes
    assert RecipeEntry(recipe="ticket_triage@v4", task_family="support.ticket_batch", risk="write_reversible") in context.recipes
    assert context.catalog.tools == {"web_search": "read"}, "工具仍由工具目录给出"
    assert (context.risk_authorized, context.effect_ceiling) == ("read", "read")
    listed = fake.calls("ListNodeTemplates")[0]
    assert dict(listed.pinned_versions.node_templates) == PINNED["node_templates"]
    assert listed.tenant == tenant_proto()
    assert all(r.tenant == tenant_proto() for r in fake.calls("ListRecipes"))


async def test_the_planning_context_lists_the_tiers_of_the_model_profiles() -> None:
    # Planner 只能写 Model Profile 里有的 Tier（2026-10-25 真模型运行写出了不存在的 strong@light）。
    fake = seeded()
    async with serve(fake) as address:
        client = GrpcRegistry(address)
        try:
            profiles = await client.list_model_profiles(TENANT)
            context = await RegistryContextSource(client, defaults=DEFAULTS).load(plan_input())
        finally:
            await client.aclose()
    assert len(profiles) == 5, "分页（每页 2 条）取完"
    assert context.catalog.tiers == ("mid@low", "small@low", "strong@high", "strong@medium"), "<class>@<tier 名>，排序去重；只算能生成的 Profile"
    assert all(r.tenant == tenant_proto() for r in fake.calls("ListModelProfiles"))


async def test_stub_templates_stay_out_of_the_real_planners_catalog() -> None:
    """桩模板（stub_ 开头）只给桩 Planner：2026-10-28 真模型实验第 3.1 条，开发 Registry 的种子把 stub_answer@v1 列给了真 Planner，
    它拿来当读文件、跑测试的节点。Stage Runner 解析模板（resolve_catalog）不经这里，桩 Planner 的 Stage 照样能跑。"""
    fake = seeded()
    fake.add(template("stub_answer", "v1", "model", "none", "generate"))
    pinned = {**PINNED, "node_templates": {**PINNED["node_templates"], "stub_answer": "v1"}}
    async with serve(fake) as address:
        source = RegistryContextSource(GrpcRegistry(address), defaults=DEFAULTS)
        context = await source.load(dataclasses.replace(plan_input(), pinned=pinned))
    assert "stub_answer@v1" not in context.catalog.templates
    assert "answer@v1" in context.catalog.templates


async def test_the_effect_ceiling_follows_the_task_constraints() -> None:
    fake = seeded()
    async with serve(fake) as address:
        source = RegistryContextSource(GrpcRegistry(address), defaults=DEFAULTS)
        context = await source.load(plan_input({"constraints": {"effect_ceiling": "write_reversible"}}))
    assert context.effect_ceiling == "write_reversible", "租户策略给 Task 的上限（写在 Task State 的 constraints 里）"


async def test_the_planning_context_fails_when_the_registry_is_unavailable() -> None:
    fake = seeded()
    fake.abort = unavailable()
    async with serve(fake) as address:
        source = RegistryContextSource(GrpcRegistry(address), defaults=DEFAULTS)
        with pytest.raises(TransientError):
            await source.load(plan_input())


# --- 组装 ---


def test_the_worker_uses_the_registry_only_when_configured() -> None:
    from agentruntime_worker.tools import DevCatalog
    from agentruntime_worker.planner.activities import dev_planner
    from agentruntime_worker.planner.context import StaticContextSource

    default = decision_activities(WorkerConfig())
    configured = decision_activities(WorkerConfig(registry_address="127.0.0.1:8092"))
    assert isinstance(default[names.RESOLVE_CATALOG].__self__, DevCatalog), "没接 Registry 时模板是桩，工具 effect 取自 ToolDriver 的登记（T-10）"
    assert isinstance(configured[names.RESOLVE_CATALOG].__self__, RegistryCatalog)
    assert set(configured) == set(default)

    # 两种都包一层 ToolsContextSource：加上 ToolDriver 登记的工具（T-13）。
    assert isinstance(dev_planner(WorkerConfig(planner="model")).contexts.inner, StaticContextSource)
    assert isinstance(dev_planner(WorkerConfig(planner="model", registry_address="127.0.0.1:8092")).contexts.inner, RegistryContextSource)


def test_the_registry_address_is_read_from_the_environment() -> None:
    assert WorkerConfig.from_env({}).registry_address == ""
    assert WorkerConfig.from_env({"AR_REGISTRY_ADDRESS": " 127.0.0.1:8092 "}).registry_address == "127.0.0.1:8092"


# --- T-09：Hook 的来源（细化见 docs/plan/policy-engine.md 第 2 节第 3 条） ---


def stage_policy_request(pinned: dict | None = None) -> StagePolicyRequest:
    return StagePolicyRequest(tenant=TENANT, task_id="tsk_reg", pinned={"tenant_policy": "v5", "recipe": "refund_analysis@v1"} if pinned is None else pinned)


def policy_registry() -> FakeRegistry:
    fake = seeded()
    fake.policies[("t-reg", "v5")] = {"effect_ceiling": "read", "hooks": {"before_node": ["pii_check"], "after_node": ["pii_redaction"]}}
    fake.recipes = [
        recipe("refund_analysis", "v1", "analytics.attribution", "read", hooks={"after_node": ["pii_redaction", "row_limit"], "before_finish": ["acceptance_recheck"]}),
    ]
    return fake


async def test_stage_policy_hooks_come_from_the_pinned_tenant_policy_and_recipe() -> None:
    fake = policy_registry()
    defaults = StagePolicy(max_parallel=3)
    async with serve(fake) as address:
        loader = RegistryStagePolicy(GrpcRegistry(address), defaults=defaults)
        policy = await ActivityEnvironment().run(loader.load_stage_policy, stage_policy_request())
    assert policy.hooks == {
        "before_node": ["pii_check"],
        "after_node": ["pii_redaction", "row_limit"],
        "before_finish": ["acceptance_recheck"],
    }, "按租户策略、Recipe 的顺序合并，同名的只留一个"
    assert policy.max_parallel == 3, "其余上限取默认"
    (asked_policy,) = fake.calls("GetTenantPolicy")
    assert asked_policy.version == "v5" and asked_policy.tenant == tenant_proto(), "按 Task 固定的版本取"
    (asked_recipe,) = fake.calls("GetRecipe")
    assert (asked_recipe.name, asked_recipe.version) == ("refund_analysis", "v1")


async def test_no_pins_means_no_policy_hooks() -> None:
    fake = policy_registry()
    async with serve(fake) as address:
        loader = RegistryStagePolicy(GrpcRegistry(address), defaults=StagePolicy())
        policy = await ActivityEnvironment().run(loader.load_stage_policy, stage_policy_request({}))
    assert policy.hooks == {}
    assert fake.requests == [], "没有固定的租户策略与 Recipe 就不问 Registry"


@pytest.mark.parametrize(("pinned", "error_type"), [({"tenant_policy": "v9"}, "tenant_policy_not_found"), ({"recipe": "ghost@v1"}, "recipe_not_found")])
async def test_a_pinned_version_that_cannot_be_found_is_not_retryable(pinned: dict, error_type: str) -> None:
    fake = policy_registry()
    async with serve(fake) as address:
        loader = RegistryStagePolicy(GrpcRegistry(address), defaults=StagePolicy())
        with pytest.raises(ApplicationError) as caught:
            await ActivityEnvironment().run(loader.load_stage_policy, stage_policy_request(pinned))
    assert caught.value.non_retryable and caught.value.type == error_type, "固定的版本取不到一律按拒绝处理（设计书 3.1.2）"


async def test_an_unavailable_registry_makes_the_stage_policy_retryable() -> None:
    fake = policy_registry()
    fake.abort = unavailable()
    async with serve(fake) as address:
        loader = RegistryStagePolicy(GrpcRegistry(address), defaults=StagePolicy())
        with pytest.raises(TransientError):
            await ActivityEnvironment().run(loader.load_stage_policy, stage_policy_request())


async def test_the_registry_catalog_is_authoritative() -> None:
    fake = seeded()
    async with serve(fake) as address:
        catalog = RegistryCatalog(GrpcRegistry(address), tools=no_tools())
        info = await ActivityEnvironment().run(catalog.resolve_catalog, catalog_request(["sql_query@v1"]))
    assert info.authoritative, "Stage Runner 据此按 Registry 核对 Atomic Node 的 executor、effect"


def test_the_worker_loads_hooks_from_the_registry_and_runs_the_real_policy_engine() -> None:
    from agentruntime_worker.activities.stubs import StubActivities
    from agentruntime_worker.policy import PolicyEngine

    default = decision_activities(WorkerConfig())
    configured = decision_activities(WorkerConfig(registry_address="127.0.0.1:8092"))
    assert isinstance(default[names.LOAD_STAGE_POLICY].__self__, StubActivities)
    assert isinstance(configured[names.LOAD_STAGE_POLICY].__self__, RegistryStagePolicy)
    for activities in (default, configured):
        assert isinstance(activities[names.RUN_HOOKS].__self__, PolicyEngine), "Worker 默认就用真的 Policy Engine"
