"""S-09：分诊与 Triage（设计书 3.2.2；附录 C.1、C.2）。规则由代码判定，分类器只给信号。"""

from __future__ import annotations

import pytest

from agentruntime_worker.planner.planner import Planner
from agentruntime_worker.planner.triage import Signals, decide

from .planner_support import CI_RECIPE, FLIGHT_RECIPE, FakeGateway, context, last_round, plan_answer, request, signals
from .support import assert_valid

THRESHOLD = 0.8


def sig(kind: str = "new_task", **kw: object) -> Signals:
    raw = signals(kind, **kw)  # type: ignore[arg-type]
    return Signals(kind=raw["kind"], task_family=raw["task_family"], confidence=raw["confidence"], effect=raw["effect"], irreversible_params_explicit=raw["irreversible_params_explicit"])


def level(signals_: Signals | None, **kw: object) -> str:
    ctx = kw.pop("ctx", context())
    return decide(signals_, context=ctx, last_round=kw.pop("last", None), confidence_threshold=THRESHOLD).level  # type: ignore[arg-type]


# --- 规则：decide 是纯函数 ---


def test_a_reviewer_replan_means_replan_whatever_the_classifier_says() -> None:
    replan = last_round({"verdict": "replan", "reason": "假设被证伪", "replan": {"keep_nodes": ["read_log"]}})
    for kind in ("follow_up", "new_task", "chitchat"):
        assert level(sig(kind), last=replan) == "replan"
    assert level(None, last=replan) == "replan"


def test_a_constraint_on_a_running_task_means_replan() -> None:
    running = last_round({"verdict": "continue", "reason": "取数完成"})
    assert level(sig("constraint", effect="read"), last=running) == "replan"


def test_without_classifier_signals_the_planner_goes_full() -> None:
    decision = decide(None, context=context(), last_round=None, confidence_threshold=THRESHOLD)
    assert decision.level == "full"
    assert decision.reasons, "判定依据要写清楚（写入 trace）"


@pytest.mark.parametrize(("authorized", "effect"), [("read", "write_reversible"), ("read", "write_irreversible"), ("write_reversible", "write_irreversible"), ("none", "read")])
def test_a_risk_above_the_authorized_level_always_goes_full(authorized: str, effect: str) -> None:
    """风险高于本 Session 已授权的等级一律 full，由代码判定：分类器说续改、命中 Recipe 都不行。"""
    ctx = context(risk=authorized, recipes=[CI_RECIPE, FLIGHT_RECIPE])
    assert level(sig("follow_up", effect=effect), ctx=ctx) == "full"
    assert level(sig("new_task", family="engineering.ci_fix", effect=effect), ctx=ctx) == "full"


def test_a_recipe_riskier_than_authorized_goes_full_even_if_the_message_looks_harmless() -> None:
    ctx = context(risk="read", recipes=[CI_RECIPE])
    assert level(sig("new_task", family="engineering.ci_fix", effect="read"), ctx=ctx) == "full"


@pytest.mark.parametrize("kind", ["follow_up", "chitchat", "question", "system_question"])
def test_follow_ups_chitchat_and_simple_questions_go_fast(kind: str) -> None:
    assert level(sig(kind, family=None, effect="none")) == "fast"


def test_a_follow_up_that_needs_an_action_is_not_fast() -> None:
    assert level(sig("follow_up", family=None, effect="read")) != "fast"


def test_a_recipe_hit_that_passes_the_delta_check_goes_light() -> None:
    ctx = context(risk="write_reversible", ceiling="write_reversible", recipes=[CI_RECIPE], budget=5.0)
    decision = decide(sig("new_task", family="engineering.ci_fix", effect="write_reversible"), context=ctx, last_round=None, confidence_threshold=THRESHOLD)
    assert decision.level == "light"
    assert decision.recipe == CI_RECIPE


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"recipes": [CI_RECIPE.__class__(**{**CI_RECIPE.__dict__, "recent_failure": True})]}, "Recipe 最近失败过"),
        ({"updated": True}, "Model Profile 已更新"),
        ({"budget": 0.1}, "剩余预算不够典型成本"),
        ({"recipes": []}, "没有命中 Recipe"),
    ],
)
def test_each_failed_delta_check_escalates_light_to_full(change: dict, why: str) -> None:
    base = {"risk": "write_reversible", "ceiling": "write_reversible", "recipes": [CI_RECIPE], "budget": 5.0}
    ctx = context(**{**base, **change})
    assert level(sig("new_task", family="engineering.ci_fix", effect="write_reversible"), ctx=ctx) == "full", why


def test_low_classifier_confidence_escalates_to_full() -> None:
    ctx = context(risk="write_reversible", ceiling="write_reversible", recipes=[CI_RECIPE], budget=5.0)
    assert level(sig("new_task", family="engineering.ci_fix", effect="write_reversible", confidence=0.5), ctx=ctx) == "full"


def test_light_with_an_irreversible_action_needs_explicit_parameters() -> None:
    """附录 C.2：light 涉及不可逆动作时，对象与参数要能从这次的指令里明确得出。"""
    ctx = context(risk="write_irreversible", recipes=[FLIGHT_RECIPE], budget=5.0)
    explicit = sig("new_task", family="travel.booking", effect="write_irreversible", explicit=True)
    vague = sig("new_task", family="travel.booking", effect="write_irreversible", explicit=False)
    assert level(explicit, ctx=ctx) == "light"
    assert level(vague, ctx=ctx) == "full"


# --- 经过 Planner：fast 与 light 不调用强模型 ---


async def test_fast_plans_one_model_node_without_a_strong_model() -> None:
    gateway = FakeGateway(triage=signals("follow_up", family=None, effect="none"))
    result = await Planner(gateway).plan(request("这几个名字再短一点", round_seq=1))
    assert result.triage_level == "fast" and result.task_plan is None
    assert [c.purpose for c in result.calls] == ["triage"]
    assert all(c.tier.split("@")[0] != "strong" for c in result.calls)
    assert not gateway.calls("plan")
    [node] = result.stage["nodes"]
    assert node["template"] == "rewrite@v1" and node["role"] == "deliver" and node["executor"] == "model"
    assert result.stage["seq"] == 1
    assert_valid("stage", result.stage)


async def test_fast_answers_questions_with_the_answer_template() -> None:
    gateway = FakeGateway(triage=signals("question", family=None, effect="none"))
    result = await Planner(gateway).plan(request("Python 里怎么把字符串转成小写？"))
    assert [n["template"] for n in result.stage["nodes"]] == ["answer@v1"]


async def test_light_expands_the_recipe_without_a_strong_model() -> None:
    ctx = context(risk="write_reversible", ceiling="write_reversible", recipes=[CI_RECIPE], budget=5.0)
    gateway = FakeGateway(triage=signals("new_task", family="engineering.ci_fix", effect="write_reversible"))
    result = await Planner(gateway).plan(request("test_invoice_total 又挂了，还按老办法修一下", ctx=ctx))
    assert result.triage_level == "light"
    assert not gateway.calls("plan")
    goal, deliver = result.stage["nodes"]
    assert goal["kind"] == "goal" and goal["mode"] == "template" and goal["recipe"] == "ci_fix@v2"
    assert deliver["role"] == "deliver" and deliver["after"] == [goal["id"]]
    assert result.task_plan["triage"]["level"] == "light" and result.task_plan["recipe"] == "ci_fix@v2"
    assert_valid("stage", result.stage)
    assert_valid("task_plan", result.task_plan)


async def test_the_classifier_runs_on_the_small_tier_with_the_tenant_and_task() -> None:
    gateway = FakeGateway(triage=signals("chitchat", family=None, effect="none"))
    await Planner(gateway).plan(request("谢谢，今天先到这里。"))
    [call] = gateway.calls("triage")
    assert call.tenant.tenant_id == "t1" and call.call.task_id
    assert call.call.tier == "small@low"
    assert call.generation.output_schema.ByteSize() > 0, "分类器用结构化输出"


@pytest.mark.parametrize("bad", [RuntimeError("超时"), "不是 JSON", {"kind": "unknown_kind"}])
async def test_a_broken_classifier_falls_back_to_full_planning(bad: object) -> None:
    gateway = FakeGateway(triage=bad, plan=[plan_answer()])
    result = await Planner(gateway).plan(request())
    assert result.triage_level == "full"
    assert len(gateway.calls("plan")) == 1
