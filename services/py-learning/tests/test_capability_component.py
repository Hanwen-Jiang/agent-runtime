"""能力矩阵的组件级评测（capability.evals.s09、s10、s11）：用仓库里的 S-09、S-10、S-11 评测集，一条用例一道题，假模型。"""

from __future__ import annotations

from typing import Any

import pytest

from agentruntime.common.v1 import enums_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

import capability_support as cs
from agentruntime_learning.capability.base import PinningGateway
from agentruntime_learning.capability.evals import load_eval
from agentruntime_learning.capability.evals.s09 import planner_input
from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET, load_eval_set
from agentruntime_learning.evals.context import ExtractiveGateway
from agentruntime_learning.evals.worker_subject import planning_context
from agentruntime_worker.planner.triage import reviewer_decision

from fake_gateway import ScriptedGateway

ES = load_eval_set(DEFAULT_EVAL_SET)


def reference_plan(request: mg.GenerateRequest) -> Any:
    """按提示里的用户消息找到用例，答它的参照 Stage 与 Task Plan（一个“规划全对”的模型）。"""
    text = "\n".join(m.text for m in request.generation.messages)
    for case in ES.task_cases:
        if case.planner_input["message"] in text and case.planner_expect["triage"] in ("full", "replan"):
            ref = case.planner_expect["reference"]
            return {"task_plan": ref["task_plan"], "stage": ref["stage"]}
    return "?"


async def run(spec: str, role: str, candidate: str, tier: str, inner_factory: Any, n: int = 200) -> list[tuple[Any, Any, cs.ServingGateway, PinningGateway]]:
    ev = load_eval(spec)
    out = []
    for item in ev.items(n, 0):
        inner = cs.ServingGateway(inner_factory())
        pin = PinningGateway(inner, role=role, candidate=candidate, tier=tier)
        out.append((item, await ev.run_item(item, pin, candidate=candidate), inner, pin))
    return out


@pytest.mark.parametrize("spec", ["s09", "s09:route", "s10", "s11"])
def test_items_are_seeded_and_prefix_stable(spec: str) -> None:
    ev = load_eval(spec)
    assert ev.role in ("plan", "route", "judge", "transform") and ev.version
    ten, more = ev.items(10, 7), ev.items(30, 7)
    assert [i.id for i in more[: len(ten)]] == [i.id for i in ten] and ten == ev.items(10, 7)
    assert [i.id for i in ev.items(10, 8)] != [i.id for i in ten] or len(ten) < 3
    assert len({i.id for i in ev.items(500, 0)}) == len(ev.items(500, 0))


async def test_the_plan_cell_pins_planning_and_scores_the_s09_decision() -> None:
    results = await run("s09", "plan", cs.OPUS, "strong@medium", lambda: ScriptedGateway(plan=reference_plan))
    expected = {c.id for c in ES.task_cases if c.planner_expect["triage"] in ("full", "replan")}
    assert {item.id for item, *_ in results} == expected, "只有 full、replan 的用例调用规划模型"
    for item, result, inner, pin in results:
        plan_calls = [r for r in inner.requests if r.call.role == enums_pb2.ROLE_PLAN]
        route_calls = [r for r in inner.requests if r.call.role == enums_pb2.ROLE_ROUTE]
        assert all(r.call.pinned_model_profile == cs.OPUS and r.call.tier == "strong@medium" for r in plan_calls)
        assert all(not r.call.pinned_model_profile and r.call.tier == "small@low" for r in route_calls), "分诊照常选档"
        assert not pin.contaminated and result.cost_usd > 0
        if result.record["triage_expected"] == "full":
            assert result.score == 1.0, (item.id, result.record["failures"])
        assert {"triage", "triage_expected", "failures", "fallback", "split"} <= set(result.record)


async def test_a_plan_model_that_answers_nonsense_scores_low() -> None:
    results = await run("s09", "plan", cs.OPUS, "strong@medium", ScriptedGateway)
    assert sum(r.score for _, r, *_ in results) / len(results) < 0.3
    assert all(r.record["fallback"] for _, r, *_ in results if r.record["triage"] in ("full", "replan"))


async def test_the_route_cell_only_asks_the_classifier() -> None:
    results = await run("s09:route", "route", cs.FLASH, "mid@low", ScriptedGateway)
    for item, result, inner, pin in results:
        case = next(c for c in ES.task_cases if c.id == item.id)
        assert reviewer_decision(case.planner_input.get("last_round"), context=planning_context(planner_input(case, ES))) is None
        assert {r.call.role for r in inner.requests} == {enums_pb2.ROLE_ROUTE}, "规划角色的调用在本地拒掉，不花钱"
        assert inner.requests[0].call.pinned_model_profile == cs.FLASH and inner.requests[0].call.tier == "mid@low"
        # 分类器答不出合格的信号 → 按 full：标注是 full 的用例才算对。
        assert result.score == (1.0 if case.planner_expect["triage"] == "full" else 0.0)
        assert "PlanningSkipped" not in result.record["model_call_failures"] and "fallback" not in result.record
        assert not pin.contaminated
    decided = [c.id for c in ES.task_cases if reviewer_decision(c.planner_input.get("last_round"), context=planning_context(planner_input(c, ES))) is not None]
    assert decided and not set(decided) & {i.id for i, *_ in results}


async def test_the_judge_cell_scores_reviewer_checkpoints() -> None:
    results = await run("s10", "judge", cs.SOL, "strong@medium", ScriptedGateway)
    rounds = sum(len(c.rounds) for c in ES.task_cases)
    assert len(results) == len(ES.reviewer_samples) + rounds
    assert len(load_eval("s10", {"checkpoints": "samples"}).items(200, 0)) == len(ES.reviewer_samples)
    for item, result, inner, pin in results:
        assert inner.requests and all(r.call.role == enums_pb2.ROLE_JUDGE and r.call.pinned_model_profile == cs.SOL for r in inner.requests)
        assert not pin.contaminated and result.record["verdict"] in ("done", "continue", "repair", "replan", "rollback", "reroute", "escalate", "abort")
        assert result.score == (1.0 if not result.record["failures"] else 0.0)
    # 模型答不出合格的 Progress Check 时只按规则判定，照样有不少判对的
    assert 0.3 < sum(r.score for _, r, *_ in results) / len(results) < 1.0


async def test_a_judge_pin_that_lands_elsewhere_is_contaminated() -> None:
    ev = load_eval("s10")
    item = ev.items(1, 0)[0]
    pin = PinningGateway(cs.ServingGateway(ScriptedGateway(), switch_pins={cs.SOL: cs.SONNET}), role="judge", candidate=cs.SOL, tier="strong@medium")
    await ev.run_item(item, pin, candidate=cs.SOL)
    assert pin.contaminated and pin.missed_calls == pin.pinned_calls > 0


class _ContextModel:
    """S-11 的假模型：摘要与 Probe 按抽取作答（ExtractiveGateway），下一步动作不调工具。"""

    def __init__(self) -> None:
        self.extractive = ExtractiveGateway()

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        if request.call.node_id.endswith("#next"):
            return mg.GenerateResponse(finish_reason=mg.FINISH_REASON_STOP)
        return await self.extractive.generate(request)


async def test_the_compression_cell_pins_the_summary_calls() -> None:
    results = await run("s11", "transform", cs.FLASH, "mid@low", _ContextModel)
    assert len(results) == 7
    for item, result, inner, pin in results:
        transform = [r for r in inner.requests if r.call.role == enums_pb2.ROLE_TRANSFORM]
        others = [r for r in inner.requests if r.call.role != enums_pb2.ROLE_TRANSFORM]
        assert transform and all(r.call.pinned_model_profile == cs.FLASH and r.call.tier == "mid@low" for r in transform), "增量摘要钉在候选上"
        assert others and not any(r.call.pinned_model_profile for r in others), "答题、下一步动作与 Compaction Probe 照常选档"
        assert result.error is None and result.record["summarized"] and 0.0 < result.score <= 1.0
        assert not result.record["repeated"] and result.record["passed"] <= result.record["probes"]
        assert ("action" in result.record) == (result.record["split"] == "dev"), "留出集不记模型给的动作"
        assert not pin.contaminated


async def test_a_broken_compression_answer_scores_zero_with_the_error() -> None:
    class Broken(_ContextModel):
        async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
            if request.call.node_id.endswith("#answer"):
                raise RuntimeError("bad json")
            return await super().generate(request)

    results = await run("s11", "transform", cs.MINI, "small@low", Broken, n=1)
    (_, result, _, _), = results
    assert result.score == 0.0 and "bad json" in result.error
