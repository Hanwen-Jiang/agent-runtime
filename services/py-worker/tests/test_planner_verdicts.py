"""S-10 对 Planner 的要求：Reviewer 的判定交回 Planner 时怎么规划（附录 B.4、B.8；设计书 4.11）。

- repair 换方法、rollback、问人后“带指导重规划”：Triage = replan，只重做剩余部分；
- repair 换方法时，修复范围（要修的 Node 及其下游）之外、上一轮成功的 Node 都保留，不得重做；
- reroute：命中 Recipe 缓存、风险不高于已授权的等级时走 light，用 Reviewer 指定的 Recipe；否则 full；
- 问人后“原样继续”：照常分诊。
"""

from __future__ import annotations

from typing import Any

from agentruntime_worker.planner.planner import Planner
from agentruntime_worker.planner.triage import Signals, decide

from .planner_support import CI_RECIPE, FakeGateway, context, last_round, plan_answer, prompt_text, request, signals

THRESHOLD = 0.8

TWO_STEP = {
    "seq": 1,
    "goal": "读日志并修复",
    "exit_when": "测试通过",
    "nodes": [
        {"id": "read_log", "kind": "atomic", "template": "run_tests@v1", "executor": "program", "effect": "read", "role": "fetch"},
        {"id": "fix", "kind": "atomic", "template": "summarize@v1", "executor": "model", "output": "generate", "effect": "none", "role": "transform", "after": ["read_log"]},
    ],
}
TWO_STEP_RESULTS = {
    "read_log": {"role": "fetch", "status": "succeeded", "result": {"locale": "C"}},
    "fix": {"role": "transform", "status": "failed", "failure_class": "invalid_output", "error": "补丁没修好"},
}


def sig(kind: str = "new_task", **kw: Any) -> Signals:
    raw = signals(kind, **kw)
    return Signals(kind=raw["kind"], task_family=raw["task_family"], confidence=raw["confidence"], effect=raw["effect"], irreversible_params_explicit=raw["irreversible_params_explicit"])


def escalated(option: str, guidance: str = "") -> dict[str, Any]:
    verdict = {"verdict": "escalate", "reason": "高风险决定", "escalate": {"target": "human", "question": "继续吗？", "options": ["continue_as_is", "replan_with_guidance", "stop_and_wrap_up"]}}
    return {**last_round(verdict), "human_reply": {"option": option, "guidance": guidance}}


CHANGE_METHOD = last_round(
    {"verdict": "repair", "reason": "同一错误第二次出现", "repair": {"nodes": ["fix"], "strategy": "change_method", "guidance": "改用格式化工具"}},
    stage=TWO_STEP,
    results=TWO_STEP_RESULTS,
)
ROLLBACK = last_round({"verdict": "rollback", "reason": "共享产物被改坏", "rollback": {"checkpoint": "round_1"}})


def test_verdicts_that_redo_part_of_the_plan_mean_replan() -> None:
    for last in (CHANGE_METHOD, ROLLBACK, escalated("replan_with_guidance", "改订 3 号")):
        for signals_ in (sig("chitchat"), sig("new_task"), None):
            assert decide(signals_, context=context(), last_round=last, confidence_threshold=THRESHOLD).level == "replan"


def test_continuing_as_is_after_a_human_reply_triages_as_usual() -> None:
    last = escalated("continue_as_is")
    assert decide(sig("new_task"), context=context(), last_round=last, confidence_threshold=THRESHOLD).level == "full"


def test_a_reroute_goes_light_on_the_named_recipe_when_it_is_cached_and_allowed() -> None:
    ctx = context(risk="write_reversible", ceiling="write_reversible", recipes=[CI_RECIPE], budget=5.0)
    reroute = last_round({"verdict": "reroute", "reason": "实际是 CI 修复流程", "reroute": {"recipe": "ci_fix@v2"}})
    decision = decide(sig("new_task", family="analytics.attribution"), context=ctx, last_round=reroute, confidence_threshold=THRESHOLD)
    assert decision.level == "light" and decision.recipe is not None and decision.recipe.recipe == "ci_fix@v2"
    riskier = context(risk="read", ceiling="write_reversible", recipes=[CI_RECIPE], budget=5.0)
    assert decide(sig("new_task"), context=riskier, last_round=reroute, confidence_threshold=THRESHOLD).level == "full", "风险高于已授权的等级一律 full"
    unknown = last_round({"verdict": "reroute", "reason": "换流程", "reroute": {"recipe": "made_up@v1"}})
    assert decide(sig("new_task"), context=ctx, last_round=unknown, confidence_threshold=THRESHOLD).level == "full"


async def test_change_method_keeps_the_nodes_outside_the_repaired_part() -> None:
    """repair 换方法：read_log 在修复范围之外、上一轮成功了，新 Stage 不得重做它；重做了就带着问题重问。"""
    redo = plan_answer(
        {"id": "read_log", "kind": "atomic", "template": "run_tests@v1", "executor": "program", "effect": "read", "role": "fetch"},
        level="replan",
    )
    good = plan_answer(
        {"id": "fix_v2", "kind": "atomic", "template": "summarize@v1", "executor": "model", "output": "generate", "effect": "none", "role": "transform"},
        level="replan",
    )
    gateway = FakeGateway(plan=[redo, good])
    result = await Planner(gateway).plan(request("修好 CI", round_seq=2, last_round=CHANGE_METHOD))
    assert result.triage_level == "replan"
    assert [n["id"] for n in result.stage["nodes"]] == ["fix_v2"]
    plans = gateway.calls("plan")
    assert len(plans) == 2 and "read_log" in prompt_text(plans[1])
    assert "改用格式化工具" in prompt_text(plans[0]), "换方法的指导要给 Planner"


async def test_the_humans_guidance_reaches_the_planner() -> None:
    gateway = FakeGateway(plan=[plan_answer(level="replan")])
    await Planner(gateway).plan(request("订机票", round_seq=2, last_round=escalated("replan_with_guidance", "改订 3 号的航班")))
    assert "改订 3 号的航班" in prompt_text(gateway.calls("plan")[0])
