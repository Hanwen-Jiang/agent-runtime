"""S-09：Planner 的 decision activity，以及 fast 与 light 增加的延迟（计划 S-09 难点“规则优先”）。"""

from __future__ import annotations

import time
from typing import Any

from temporalio.testing import ActivityEnvironment

from agentruntime_worker.activities.types import PlanRoundInput, PlanRoundOutput
from agentruntime_worker.planner.activities import PlannerActivities
from agentruntime_worker.planner.context import StaticContextSource
from agentruntime_worker.planner.planner import Planner

from .planner_support import CI_RECIPE, TENANT, FakeGateway, context, last_round, plan_answer, request, signals
from .support import assert_valid

STATE = {"task_id": "tsk_01J" + "0" * 22 + "P", "session_id": "ses_01J" + "0" * 22 + "S", "goal": "分析退款率", "done_when": "结论有数据支撑", "rounds": []}


def plan_input(message: str = "分析上季度退款率上升的原因", **kw: Any) -> PlanRoundInput:
    return PlanRoundInput(tenant=TENANT, task_id=STATE["task_id"], round_seq=kw.pop("round_seq", 1), task_state=STATE, message=message, inputs={}, attachments=[], pinned={}, **kw)


async def test_plan_round_returns_the_stage_triage_level_and_task_plan() -> None:
    gateway = FakeGateway(triage=signals(confidence=0.6), plan=[plan_answer()])
    activities = PlannerActivities(Planner(gateway), StaticContextSource(context()))
    out = await ActivityEnvironment().run(activities.plan_round, plan_input())
    assert isinstance(out, PlanRoundOutput)
    assert out.triage_level == "full"
    assert_valid("stage", out.stage)
    assert_valid("task_plan", out.task_plan)


async def test_plan_round_loads_the_context_for_the_requests_tenant() -> None:
    gateway = FakeGateway(triage=signals("new_task", family="engineering.ci_fix", effect="write_reversible"))
    light_ctx = context(risk="write_reversible", ceiling="write_reversible", recipes=[CI_RECIPE], budget=5.0)
    source = StaticContextSource(context(), by_tenant={"t1": light_ctx})
    out = await ActivityEnvironment().run(PlannerActivities(Planner(gateway), source).plan_round, plan_input("还按老办法修一下"))
    assert out.triage_level == "light", "t1 的上下文里有 Recipe 缓存"


async def test_plan_round_passes_the_previous_round_and_task_plan_to_the_planner() -> None:
    gateway = FakeGateway(plan=[plan_answer(level="replan")])
    replan = last_round({"verdict": "replan", "reason": "假设被证伪", "replan": {"keep_nodes": ["read_log"]}})
    prior = {"triage": {"level": "full", "task_family": "analytics.attribution"}, "horizon": "per_stage"}
    out = await ActivityEnvironment().run(
        PlannerActivities(Planner(gateway), StaticContextSource(context())).plan_round,
        plan_input(round_seq=2, task_plan=prior, last_round=replan),
    )
    assert out.triage_level == "replan" and out.stage["seq"] == 2


async def test_fast_and_light_add_little_latency_beyond_the_classifier() -> None:
    """fast 与 light 增加的延迟控制在数百毫秒内：除去模型本身的时间，Planner 自己的开销要小。"""
    light_ctx = context(risk="write_reversible", ceiling="write_reversible", recipes=[CI_RECIPE], budget=5.0)
    cases = [
        (signals("follow_up", family=None, effect="none"), context(), "再短一点"),
        (signals("new_task", family="engineering.ci_fix", effect="write_reversible"), light_ctx, "还按老办法修一下"),
    ]
    for triage, ctx, message in cases:
        gateway = FakeGateway(triage=triage, delay_s=0.05)
        planner = Planner(gateway)
        await planner.plan(request(message, ctx=ctx))  # 预热：schema 校验器等只建一次
        runs = 10
        start = time.perf_counter()
        for _ in range(runs):
            await planner.plan(request(message, ctx=ctx))
        overhead = (time.perf_counter() - start - gateway.model_seconds + 0.05) / runs
        assert overhead < 0.1, f"{message}：Planner 自己的开销 {overhead * 1000:.0f} 毫秒"
        assert not gateway.calls("plan")
