"""Planner 的 Tier：只能用 Model Profile 里有的（2026-10-25 真模型运行里 Planner 写了不存在的 strong@light，
网关报 no_eligible_model，Reviewer 来回 repair、replan 四轮）。

- 提示列出可用的 Tier，并说明 Triage 的级别不是 Tier 名；不知道有哪些 Tier 时让模型不写。
- 规划结果里不在列表中的 tier（含格式不对的）由代码去掉，Model Gateway 按 role 取默认 Tier（附录 H.3）；
  不为它重问模型，去掉了什么记在 PlanResult.adjustments（写入 trace）。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from agentruntime_worker.planner.planner import Planner

from .planner_support import CATALOG, FakeGateway, context, plan_answer, prompt_text, request, signals
from .support import assert_valid

RISKY_FULL = signals("new_task", family="analytics.attribution", confidence=0.6, effect="read")
TIERS = ("mid@low", "small@low", "strong@high", "strong@medium")


def with_tiers(tiers: tuple[str, ...] = TIERS):
    return dataclasses.replace(context(), catalog=dataclasses.replace(CATALOG, tiers=tiers))


def model_node(node_id: str, tier: str | None, role_: str = "deliver") -> dict[str, Any]:
    node: dict[str, Any] = {"id": node_id, "kind": "atomic", "template": "summarize@v1", "executor": "model", "output": "generate", "effect": "none", "role": role_}
    if tier is not None:
        node["tier"] = tier
    return node


async def plan(gateway: FakeGateway, ctx):
    return await Planner(gateway).plan(request(ctx=ctx))


# --- 提示 ---


async def test_the_prompt_lists_the_available_tiers() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer()])
    await plan(gateway, with_tiers())
    [call] = gateway.calls("plan")
    system = call.generation.messages[0].text
    assert "Tier：mid@low、small@low、strong@high、strong@medium" in system, "可用的 Tier 放在 system 区（与 Node Catalog 一起，缓存友好）"
    assert "不是 Tier 名" in system, "说明 Triage 的级别（fast、light…）不是 Tier 名"


async def test_the_rules_explain_the_model_preference() -> None:
    # coding-web 把用户选的模型倾向放进 inputs.constraints.model_preference，进 Task State 的约束。
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer()])
    await Planner(gateway).plan(request(ctx=with_tiers(), task_state={"goal": "修测试", "constraints": {"model_preference": "economy"}}))
    [call] = gateway.calls("plan")
    assert "model_preference" in call.generation.messages[0].text
    assert '"model_preference":"economy"' in prompt_text(call), "Task State（含约束）原样给 Planner"


async def test_without_known_tiers_the_prompt_says_not_to_write_one() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer()])
    await plan(gateway, context())
    [call] = gateway.calls("plan")
    assert "不要写 tier" in prompt_text(call)


# --- 规划结果 ---


async def test_an_unknown_tier_is_dropped_so_the_gateway_uses_the_role_default() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer(model_node("deliver", "strong@light"))])
    result = await plan(gateway, with_tiers())
    assert not result.fallback
    assert len(gateway.calls("plan")) == 1, "不为档位重问模型，直接改正"
    [node] = result.stage["nodes"]
    assert "tier" not in node, "去掉 tier，Model Gateway 按 role 取默认 Tier"
    assert_valid("stage", result.stage)
    assert any("deliver" in a and "strong@light" in a for a in result.adjustments), result.adjustments


async def test_a_listed_tier_is_kept() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer(model_node("deliver", "strong@high"))])
    result = await plan(gateway, with_tiers())
    [node] = result.stage["nodes"]
    assert node["tier"] == "strong@high"
    assert result.adjustments == []


async def test_a_malformed_tier_is_dropped_without_asking_again() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer(model_node("deliver", "Strong-Light"))])
    result = await plan(gateway, with_tiers())
    assert len(gateway.calls("plan")) == 1 and not result.fallback
    assert "tier" not in result.stage["nodes"][0]


async def test_every_tier_is_dropped_when_none_are_known() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer(model_node("deliver", "strong@medium"))])
    result = await plan(gateway, context())
    assert "tier" not in result.stage["nodes"][0], "不知道有哪些 Tier 时一律交给 Model Gateway 按 role 选"


async def test_unknown_task_plan_tiers_are_dropped() -> None:
    answer = plan_answer()
    answer["task_plan"]["tiers"] = {"planner": "strong@light", "reviewer": "strong@medium", "node_default": "mid@light"}
    gateway = FakeGateway(triage=RISKY_FULL, plan=[answer])
    result = await plan(gateway, with_tiers())
    assert result.task_plan["tiers"] == {"reviewer": "strong@medium"}
    assert_valid("task_plan", result.task_plan)
    assert len(result.adjustments) == 2


async def test_task_plan_tiers_disappear_when_none_are_left() -> None:
    answer = plan_answer()
    answer["task_plan"]["tiers"] = {"node_default": "mid@light"}
    gateway = FakeGateway(triage=RISKY_FULL, plan=[answer])
    result = await plan(gateway, with_tiers())
    assert "tiers" not in result.task_plan
