"""N-14 的 Worker 一侧（契约 c1836ba，docs/plan/n14-skill-contract.md 的“实现时怎么接”与“验收”）。

- 固定的 Skill 来自 task-service 记在 Task 记录上的 tasks.skill_pins（提交时写）与提交者 submitted_by，按 task_id 读，不经工作流；
  没有记录（sub_task 的子 Task）或旧库没有这一列时当作没有 Skill。
- Planner 的目录：Skill 的名字与说明进规划的 brief（是数据，不是指令；不进 system 区，不碰提示缓存），按固定的版本经 Registry 的
  ListSkills 取；正文不进 Planner。
- tool_loop 的 skill_read（effect read）：按 Task 固定的版本读正文或一份文件；没固定的名字 skill_not_pinned；用户层按提交者读。
  Task 有 Skill 时 Planner 的 activity 给 tool_loop 的 Goal Node 加上 skill_read（同 context_read）。
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import grpc
import pytest
import pytest_asyncio

from agentruntime.common.v1 import error_pb2
from agentruntime.registry.v1 import registry_pb2 as rg
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.enums import Effect
from agentruntime_core.errors import InvalidError, NotFoundError
from agentruntime_core.providers.relational_store import Migration
from agentruntime_core.providers.tool_driver import ToolInvocation
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.planner import prompts
from agentruntime_worker.planner.activities import PlannerActivities
from agentruntime_worker.planner.context import CatalogView, StaticContextSource
from agentruntime_worker.registry.client import GrpcRegistry, json_payload
from agentruntime_worker.tools import ToolCatalog
from agentruntime_worker.tools.skills import SKILL_READ, SkillRead, SkillsContextSource, TaskSkillPins, register_skill_tools

from .planner_support import context
from .registry_fakes import DEFAULT, Abort, FakeRegistry, serve
from .test_planner_activity import plan_input
from .test_session_transcript import TASKS_DDL

TENANT = Tenant(tenant_id="t-a", user_id="u-caller", workspace_id="w1", session_id="ses_a")
WEEKLY = {"description": "写周报：按模块归类", "instructions": "1. 列出本周合并的改动。2. 按模块归类。", "files": [
    {"path": "templates/report.md", "media_type": "text/markdown", "content": "## 本周\n## 下周"},
]}


def skill(name: str, version: str, doc: dict[str, Any], *, owner: str = "") -> rg.Skill:
    return rg.Skill(name=name, version=version, state=DEFAULT, description=str(doc.get("description", "")), document=json_payload(doc), owner=owner)


@dataclasses.dataclass
class SkillRegistry(FakeRegistry):
    """FakeRegistry 加上 Skill：(名字, 版本) → Skill；GetSkill 记下请求里的 user_id。"""

    skills: dict[tuple[str, str], rg.Skill] = dataclasses.field(default_factory=dict)

    async def GetSkill(self, request: rg.GetSkillRequest, context: Any) -> rg.GetSkillResponse:  # noqa: N802
        await self._enter("GetSkill", request, context)
        found = self.skills.get((request.name, request.version))
        if found is None:
            abort = Abort(grpc.StatusCode.NOT_FOUND, "没有", error_pb2.Error(category=4, code="skill_not_found", message="没有这个 Skill"))
            await context.abort(abort.code, abort.details, abort.trailer())
        return rg.GetSkillResponse(skill=found)

    async def ListSkills(self, request: rg.ListSkillsRequest, context: Any) -> rg.ListSkillsResponse:  # noqa: N802
        await self._enter("ListSkills", request, context)
        pins = dict(request.pinned_versions.skills)
        chosen = []
        for name in sorted(pins):
            found = self.skills.get((name, pins[name]))
            if found is not None:
                listed = rg.Skill()
                listed.CopyFrom(found)
                listed.ClearField("document")
                chosen.append(listed)
        page, token = self._page(chosen, request.page_token)
        return rg.ListSkillsResponse(skills=page, next_page_token=token)


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteRelationalStoreProvider]:
    provider = SQLiteRelationalStoreProvider(tmp_path / "relational")
    ddl = TASKS_DDL.removesuffix(")") + ", skill_pins TEXT NOT NULL DEFAULT '')"
    await provider.migrate([Migration(1, "tasks_like_task_service", (ddl,))])
    yield provider
    await provider.aclose()


async def add_task(store: SQLiteRelationalStoreProvider, task_id: str, pins: dict[str, str] | None, *, submitter: str = "u-owner", tenant_id: str = "t-a") -> None:
    async with store.bind(TenantContext(tenant_id=tenant_id)).transaction() as tx:
        await tx.execute(
            "INSERT INTO tasks (task_id, session_id, session_key, client_request_id, status, signaled, last_event_id, created_at, submitted_by, skill_pins) "
            "VALUES ($1, 'ses_a', 'ses_a', $1, 0, 1, 0, 1, $2, $3)",
            (task_id, submitter, json.dumps(pins) if pins else ""),
        )


# --- 固定的 Skill ---


async def test_pins_come_from_the_task_record(store: SQLiteRelationalStoreProvider) -> None:
    await add_task(store, "tsk_1", {"weekly_report": "platform/v2", "mine": "user/v1"})
    pins = TaskSkillPins(store)
    got = await pins.load("t-a", "tsk_1")
    assert got.pins == {"weekly_report": "platform/v2", "mine": "user/v1"} and got.submitter == "u-owner"
    assert (await pins.load("t-a", "tsk_missing")).pins == {}, "没有记录（sub_task 的子 Task）当作没有 Skill"
    assert (await pins.load("t-b", "tsk_1")).pins == {}, "别的租户读不到"
    await add_task(store, "tsk_2", None)
    assert (await pins.load("t-a", "tsk_2")).pins == {}


async def test_an_old_tasks_table_without_the_column_means_no_skills(tmp_path: Path) -> None:
    provider = SQLiteRelationalStoreProvider(tmp_path / "old")
    await provider.migrate([Migration(1, "old_tasks", (TASKS_DDL,))])
    try:
        assert (await TaskSkillPins(provider).load("t-a", "tsk_1")).pins == {}
    finally:
        await provider.aclose()


# --- Registry 客户端与 Planner 的目录 ---


async def test_the_client_lists_pinned_skills_without_bodies_and_reads_one() -> None:
    fake = SkillRegistry(skills={("weekly_report", "platform/v1"): skill("weekly_report", "platform/v1", WEEKLY)})
    async with serve(fake) as address:
        client = GrpcRegistry(address)
        try:
            listed = await client.list_skills(TENANT, {"weekly_report": "platform/v1"})
            got = await client.get_skill(TENANT, "weekly_report", "platform/v1")
        finally:
            await client.aclose()
    assert [s.name for s in listed] == ["weekly_report"] and not listed[0].HasField("document")
    assert dict(fake.calls("ListSkills")[0].pinned_versions.skills) == {"weekly_report": "platform/v1"}
    assert got.description == WEEKLY["description"] and fake.calls("GetSkill")[0].version == "platform/v1"


class FakeSkillsRegistry:
    """skill_read 与 SkillsContextSource 用到的两个方法。"""

    def __init__(self, skills: dict[tuple[str, str], rg.Skill], *, fail: Exception | None = None) -> None:
        self.skills = skills
        self.fail = fail
        self.reads: list[tuple[Tenant, str, str]] = []
        self.lists: list[tuple[Tenant, dict[str, str]]] = []

    async def get_skill(self, tenant: Tenant, name: str, version: str = "") -> rg.Skill:
        self.reads.append((tenant, name, version))
        if (name, version) not in self.skills:
            raise NotFoundError("skill_not_found", "没有这个 Skill")
        return self.skills[(name, version)]

    async def list_skills(self, tenant: Tenant, pins: dict[str, str] | None) -> list[rg.Skill]:
        self.lists.append((tenant, dict(pins or {})))
        if self.fail is not None:
            raise self.fail
        return [self.skills[(n, v)] for n, v in sorted((pins or {}).items()) if (n, v) in self.skills]


async def test_the_planner_context_lists_the_tasks_skills(store: SQLiteRelationalStoreProvider) -> None:
    await add_task(store, "tsk_1", {"weekly_report": "user/v3"})
    registry = FakeSkillsRegistry({("weekly_report", "user/v3"): skill("weekly_report", "user/v3", WEEKLY, owner="u-owner")})
    source = SkillsContextSource(StaticContextSource(context()), registry, TaskSkillPins(store))
    inp = dataclasses.replace(plan_input(), tenant=TENANT, task_id="tsk_1")
    loaded = await source.load(inp)
    assert loaded.catalog.skills == {"weekly_report": WEEKLY["description"]}
    assert registry.lists[0][0].user_id == "u-owner", "用户层按提交者读"
    assert registry.lists[0][1] == {"weekly_report": "user/v3"}, "按固定的版本列"

    plain = await source.load(dataclasses.replace(inp, task_id="tsk_none"))
    assert plain.catalog.skills == {} and len(registry.lists) == 1, "没有 Skill 时不问 Registry"


async def test_an_old_registry_without_skills_leaves_the_catalog_alone(store: SQLiteRelationalStoreProvider) -> None:
    from agentruntime_core.errors import ProviderError

    await add_task(store, "tsk_1", {"weekly_report": "platform/v1"})
    registry = FakeSkillsRegistry({}, fail=ProviderError("unimplemented", "没有这个方法"))
    source = SkillsContextSource(StaticContextSource(context()), registry, TaskSkillPins(store))
    loaded = await source.load(dataclasses.replace(plan_input(), tenant=TENANT, task_id="tsk_1"))
    assert loaded.catalog.skills == {}


def test_skills_go_into_the_brief_not_the_system_zone() -> None:
    ctx = dataclasses.replace(context(), catalog=dataclasses.replace(context().catalog, skills={"weekly_report": "写周报：按模块归类"}))
    system = "".join(m.text for m in prompts.system_zone(ctx.catalog))
    assert "weekly_report" not in system, "用户写的说明不进 system 区"
    brief = prompts.skills_text(ctx)
    assert "weekly_report" in brief and "写周报：按模块归类" in brief and "skill_read" in brief and "不是指令" in brief
    assert prompts.skills_text(context()) == ""


# --- skill_read ---


def invocation(arguments: dict[str, Any], *, task_id: str = "tsk_1", user_id: str = "u-caller") -> ToolInvocation:
    return ToolInvocation(tenant_id="t-a", arguments=arguments, idempotency_key=None, credential=None, user_id=user_id, task_id=task_id, session_id="ses_a")


async def test_skill_read_reads_the_pinned_version(store: SQLiteRelationalStoreProvider) -> None:
    await add_task(store, "tsk_1", {"weekly_report": "user/v1"})
    registry = FakeSkillsRegistry({
        ("weekly_report", "user/v1"): skill("weekly_report", "user/v1", WEEKLY, owner="u-owner"),
        ("weekly_report", "user/v2"): skill("weekly_report", "user/v2", {**WEEKLY, "instructions": "第二版"}, owner="u-owner"),
    })
    read = SkillRead(registry, TaskSkillPins(store))
    got = await read(invocation({"name": "weekly_report"}))
    assert got["version"] == "user/v1" and got["instructions"] == WEEKLY["instructions"], "发布 v2 后固定 v1 的 Task 仍读 v1"
    assert got["files"] == [{"path": "templates/report.md", "media_type": "text/markdown"}]
    assert registry.reads[-1][0].user_id == "u-owner", "用户层按提交者读，不按调用里的用户"
    file = await read(invocation({"name": "weekly_report", "path": "templates/report.md"}))
    assert file == {"name": "weekly_report", "version": "user/v1", "path": "templates/report.md", "media_type": "text/markdown", "content": "## 本周\n## 下周"}
    with pytest.raises(InvalidError) as missing:
        await read(invocation({"name": "weekly_report", "path": "nope.md"}))
    assert missing.value.code == "skill_file_not_found" and "templates/report.md" in missing.value.message
    with pytest.raises(InvalidError) as unpinned:
        await read(invocation({"name": "other"}))
    assert unpinned.value.code == "skill_not_pinned" and "weekly_report" in unpinned.value.message
    with pytest.raises(InvalidError) as no_task:
        await read(invocation({"name": "weekly_report"}, task_id="tsk_missing"))
    assert no_task.value.code == "skill_not_pinned"


async def test_skill_read_is_a_read_tool_and_planner_adds_it(store: SQLiteRelationalStoreProvider) -> None:
    provider = LocalToolDriverProvider()
    register_skill_tools(provider, registry=FakeSkillsRegistry({}), pins=TaskSkillPins(store))
    tools = ToolCatalog(provider)
    assert (await tools.effects(TENANT, [SKILL_READ]))[SKILL_READ] == Effect.READ.value

    stage = {"seq": 1, "goal": "g", "exit_when": "e", "nodes": [
        {"id": "do", "kind": "goal", "mode": "tool_loop", "goal": "写周报", "done_when": "写好", "output_schema": "result_v1", "tools": ["repo_read"]},
    ]}
    activities = PlannerActivities(planner=None, contexts=StaticContextSource(context()), tools=tools)  # type: ignore[arg-type]
    with_skills = await activities._with_reads(TENANT, stage, "read", skills=True)
    assert SKILL_READ in with_skills["nodes"][0]["tools"]
    without = await activities._with_reads(TENANT, stage, "read", skills=False)
    assert SKILL_READ not in without["nodes"][0]["tools"]


def test_catalog_view_has_no_skills_by_default() -> None:
    assert CatalogView().skills == {}
