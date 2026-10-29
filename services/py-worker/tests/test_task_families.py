"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 6 节验收（Worker 一侧）：任务族的受控词表。

细化见该计划第 11.8 节：
- Registry 按租户给出任务族词表，租户没有自己的就是平台默认的那份；Worker 经 GetTaskFamilies 读，读不到（NotFound）
  时规划上下文里没有词表，不算失败。
- 有词表时任务族由服务端定：分诊只能从词表里选，选不出记 general.unclassified；规划模型在 Task Plan 里写的不算数。
  分诊的提示列出词表里的 id 与说明。没有词表时照旧（取分诊给的）。
- PlanRoundOutput.task_family 带出这一轮定的任务族（fast 没有 Task Plan 也有）；Task 工作流收尾写回用它。
- 开发种子（deploy/dev/registry-seed.json）里有平台默认的词表。
Go 一侧（Registry 的发布、校验与读取）见 services/go-task-service/internal/registry/tmp_task_families_test.go。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from agentruntime.common.v1 import enums_pb2

from agentruntime_worker.planner.activities import PlannerActivities
from agentruntime_worker.planner import context as planning
from agentruntime_worker.planner.context import StaticContextSource
from agentruntime_worker.planner.planner import Planner
from agentruntime_worker.planner.triage import UNCLASSIFIED_TASK_FAMILY
from agentruntime_worker.registry import GrpcRegistry, RegistryContextSource

from .planner_support import FakeGateway, context, plan_answer, request, signals
from .registry_fakes import serve
from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .support import assert_valid
from .test_planner_activity import plan_input as planner_input
from .test_registry import DEFAULTS, TENANT, plan_input, seeded
from .test_stage_runner import deliver, one_round

REPO = Path(__file__).resolve().parents[3]
VOCABULARY = next(e["instance"] for e in json.loads((REPO / "schemas" / "examples" / "task_families.examples.json").read_text("utf-8"))["valid"])
OWN = {"families": [{"id": "finance.reconcile", "description": "对账"}]}


def vocabulary_context(**kw: Any) -> Any:
    import dataclasses

    families = tuple(planning.TaskFamily(id=f["id"], description=f["description"], examples=tuple(f.get("examples") or ())) for f in VOCABULARY["families"])
    return dataclasses.replace(context(**kw), task_families=families)


# --- Registry 客户端与规划上下文 ---


async def test_the_client_reads_the_tenants_vocabulary_or_else_the_platform_default() -> None:
    fake = seeded()
    fake.task_families = {"": VOCABULARY, "t2": OWN}
    async with serve(fake) as address:
        client = GrpcRegistry(address)
        platform = await client.get_task_families(TENANT)
        assert platform is not None and platform.platform_default and platform.version == "default/v1"
        assert [f.id for f in platform.families] == [f["id"] for f in VOCABULARY["families"]]
        assert fake.calls("GetTaskFamilies")[0].tenant.tenant_id == TENANT.tenant_id, "带着请求的租户"


async def test_the_planning_context_lists_the_vocabulary() -> None:
    fake = seeded()
    fake.task_families = {"": VOCABULARY}
    async with serve(fake) as address:
        ctx = await RegistryContextSource(GrpcRegistry(address), defaults=DEFAULTS).load(plan_input())
    assert ctx.task_families is not None
    assert "engineering.ci_fix" in [f.id for f in ctx.task_families]
    assert all(f.description for f in ctx.task_families)


async def test_without_a_vocabulary_the_context_has_none() -> None:
    fake = seeded()
    async with serve(fake) as address:
        ctx = await RegistryContextSource(GrpcRegistry(address), defaults=DEFAULTS).load(plan_input())
    assert ctx.task_families is None, "Registry 里还没有词表：不算失败，照旧规划"


# --- Planner：服务端定任务族 ---


def triage_text(gateway: FakeGateway) -> str:
    req = next(r for r in gateway.requests if r.call.node_id.startswith("triage"))
    return "\n".join(m.text for m in req.generation.messages)


async def test_with_a_vocabulary_the_classified_family_wins_over_the_planning_model() -> None:
    gateway = FakeGateway(triage=signals(family="engineering.ci_fix", confidence=0.6), plan=[plan_answer()])
    result = await Planner(gateway).plan(request("CI 挂了，帮我修", ctx=vocabulary_context()))
    assert result.task_plan["triage"]["task_family"] == "engineering.ci_fix", "规划模型写的 analytics.attribution 不算数"
    assert result.task_family == "engineering.ci_fix"
    text = triage_text(gateway)
    assert "engineering.ci_fix" in text and "修复失败的 CI、测试或构建" in text, "分诊的提示列出词表的 id 与说明"


async def test_a_family_outside_the_vocabulary_is_unclassified() -> None:
    gateway = FakeGateway(triage=signals(family="engineering.ci_fixes", confidence=0.6), plan=[plan_answer()])
    result = await Planner(gateway).plan(request("CI 挂了，帮我修", ctx=vocabulary_context()))
    assert result.task_plan["triage"]["task_family"] == UNCLASSIFIED_TASK_FAMILY == result.task_family


async def test_without_a_vocabulary_the_classifier_family_is_kept() -> None:
    gateway = FakeGateway(triage=signals(family="engineering.ci_fixes", confidence=0.6), plan=[plan_answer()])
    result = await Planner(gateway).plan(request("CI 挂了，帮我修"))
    assert result.task_plan["triage"]["task_family"] == "analytics.attribution", "没有词表时照旧：Task Plan 里有就用它"


@pytest.mark.parametrize(("family", "expected"), [("conversation.chat", "conversation.chat"), (None, UNCLASSIFIED_TASK_FAMILY)])
async def test_a_fast_plan_still_reports_its_family(family: str | None, expected: str) -> None:
    gateway = FakeGateway(triage=signals("follow_up", family=family, effect="none"))
    activities = PlannerActivities(Planner(gateway), StaticContextSource(vocabulary_context()))
    out = await ActivityEnvironment().run(activities.plan_round, planner_input("再短一点"))
    assert out.triage_level == "fast" and out.task_plan is None
    assert out.task_family == expected


# --- 工作流：收尾写回用服务端定的任务族 ---


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_the_wrap_up_uses_the_family_the_planner_reported(harness: StageHarness) -> None:
    s = harness.script
    s.task_family = "engineering.ci_fix"
    s.task_plan = {"triage": {"level": "full", "task_family": "analytics.attribution"}, "horizon": "per_stage"}
    one_round(harness, deliver("answer"))
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    [finalized] = [f for f in s.finalized if f.task_id == inp.task_id]
    assert finalized.task_family == "engineering.ci_fix", "服务端定的任务族优先于 Task Plan 里写的"


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_fast_task_is_written_back_with_its_family(harness: StageHarness) -> None:
    s = harness.script
    s.task_family = "conversation.chat"
    one_round(harness, deliver("answer"))
    inp, _ = await harness.run()
    [finalized] = [f for f in s.finalized if f.task_id == inp.task_id]
    assert finalized.task_family == "conversation.chat", "fast 没有 Task Plan，也有任务族"


# --- 开发种子 ---


def test_the_dev_seed_has_a_platform_default_vocabulary() -> None:
    seed = json.loads((REPO / "deploy" / "dev" / "registry-seed.json").read_text("utf-8"))
    entries = [e for e in seed["entries"] if e["kind"] == "task_families"]
    assert [e["name"] for e in entries] == ["default"] and not entries[0].get("tenant")
    doc = entries[0]["document"]
    assert_valid("task_families", doc)
    ids = {f["id"] for f in doc["families"]}
    assert {"engineering.ci_fix", "engineering.code_explain", "conversation.chat"} <= ids
