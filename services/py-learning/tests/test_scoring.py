"""S-08 评分器：结构校验、规则检查（由代码判定）、Triage 与形状特征、Verdict 与升级顺序。"""

from __future__ import annotations

from typing import Any

import pytest

from agentruntime_learning.evals import scoring
from agentruntime_learning.evals.cases import load_eval_set
from agentruntime_learning.evals.model import PlannerOutput, ReviewerOutput
from agentruntime_learning.evals.scoring import escalation_step, score_planner, score_reviewer, stage_features

from support import answer_stage, atomic, checkpoint, goal, reviewer_sample, stage, task_case, task_state, usage, verdict


def load_case(make_set, case: dict[str, Any]):
    es = load_eval_set(make_set([case]))
    return es.task_cases[0], es.catalog


def load_sample(make_set, sample: dict[str, Any]):
    return load_eval_set(make_set([task_case("t_filler")], [sample])).reviewer_samples[0]


def plan(triage: str, stage_: dict[str, Any] | None = None, *, task_plan: dict[str, Any] | None = None, cost: list | None = None) -> PlannerOutput:
    if task_plan is None and triage != "fast":
        task_plan = {"triage": {"level": triage}, "horizon": "per_stage"}
    return PlannerOutput(triage_level=triage, stage=stage_ or answer_stage(), task_plan=task_plan, usage=cost or [])


def codes(score, dimension: str | None = None) -> set[str]:
    return {f.code for f in score.failures if dimension is None or f.dimension == dimension}


# --- Planner：参照输出、结构校验 ---


def test_the_reference_output_passes(make_set) -> None:
    case, catalog = load_case(make_set, task_case("t_ok"))
    score = score_planner(case, plan("full"), catalog)
    assert score.passed and score.failures == [] and score.schema_ok


def test_a_stage_failing_the_schema_fails_the_decision(make_set) -> None:
    case, catalog = load_case(make_set, task_case("t_schema"))
    node = atomic("answer", "answer@v1", executor="model", effect="none", role="deliver")
    del node["role"]
    score = score_planner(case, plan("full", stage(node)), catalog)
    assert not score.passed and not score.schema_ok
    assert "stage_schema" in codes(score, "schema")


def test_a_stage_with_a_bad_dag_fails_the_schema_check(make_set) -> None:
    case, catalog = load_case(make_set, task_case("t_dag"))
    cyclic = stage(atomic("a", "sql_query@v1", after=["b"]), atomic("b", "sql_query@v1", after=["a"]))
    score = score_planner(case, plan("full", cyclic), catalog)
    assert "stage_dag" in codes(score, "schema") and not score.passed


def test_idempotency_keys_are_filled_by_code_before_the_schema_check(make_set) -> None:
    """幂等键由代码生成后填入，不由 Planner 编写（附录 D.4）：Planner 没写幂等键不算结构错误。"""
    case, catalog = load_case(make_set, task_case("t_keys", risk="write_reversible"))
    write = atomic("save", "save_draft@v1", effect="write_reversible", role="act")
    deliver = atomic("answer", "answer@v1", executor="model", effect="none", role="deliver", after=["save"])
    score = score_planner(case, plan("full", stage(write, deliver)), catalog)
    assert score.schema_ok, score.failures


def test_a_task_plan_failing_the_schema_fails_the_decision(make_set) -> None:
    case, catalog = load_case(make_set, task_case("t_plan_schema", triage="light", recipe="refund_analysis@v3", recipes=[{"recipe": "refund_analysis@v3", "task_family": "analytics.attribution"}]))
    score = score_planner(case, plan("light", task_plan={"triage": {"level": "light"}, "horizon": "per_stage"}), catalog)
    assert "task_plan_schema" in codes(score, "schema")


def test_the_task_plan_level_must_agree_with_the_triage_level(make_set) -> None:
    case, catalog = load_case(make_set, task_case("t_inconsistent"))
    score = score_planner(case, plan("full", task_plan={"triage": {"level": "replan"}, "horizon": "per_stage"}), catalog)
    assert "triage_inconsistent" in codes(score, "schema")


def test_full_light_and_replan_must_come_with_a_task_plan(make_set) -> None:
    case, catalog = load_case(make_set, task_case("t_no_plan"))
    score = score_planner(case, PlannerOutput(triage_level="full", stage=answer_stage(), task_plan=None), catalog)
    assert "task_plan_missing" in codes(score, "schema")


# --- Triage ---


def test_a_wrong_triage_level_fails_with_an_explanation(make_set) -> None:
    case, catalog = load_case(make_set, task_case("t_fast", triage="fast"))
    score = score_planner(case, plan("full"), catalog)
    assert not score.passed
    [failure] = [f for f in score.failures if f.dimension == "triage"]
    assert "fast" in failure.message and "full" in failure.message


# --- 规则：由代码判定，不看标注 ---


def test_rising_risk_requires_full(make_set) -> None:
    rising = task_case("t_rise", triage="full", risk="write_irreversible", risk_before="read", ceiling="write_irreversible")
    case, catalog = load_case(make_set, rising)
    for level in ("fast", "light", "replan"):
        task_plan = {"triage": {"level": level}, "horizon": "per_stage", **({"recipe": "refund_analysis@v3"} if level == "light" else {})}
        score = score_planner(case, plan(level, task_plan=None if level == "fast" else task_plan), catalog)
        assert scoring.RULE_RISK_RISE_REQUIRES_FULL in codes(score, "rule"), level
    assert scoring.RULE_RISK_RISE_REQUIRES_FULL not in codes(score_planner(case, plan("full"), catalog))


def test_no_risk_rise_means_no_rule_violation(make_set) -> None:
    same = task_case("t_same", triage="fast", risk="read", risk_before="read")
    case, catalog = load_case(make_set, same)
    assert codes(score_planner(case, plan("fast"), catalog), "rule") == set()


def test_irreversible_actions_must_be_declared_so_the_runtime_gates_them(make_set) -> None:
    """write_irreversible 前必须有 gate：运行时只在按 write_irreversible 声明的 Atomic Node 前插入人工审批（附录 F.3）。

    Goal Node（tool_loop）里的工具调用不经过这道审批，所以不可逆动作放进 Goal Node 一律算违规，不论 effect_ceiling。
    """
    case, catalog = load_case(make_set, task_case("t_pay", risk="write_irreversible", irreversible=["pay_order@v1", "card_charge"]))
    under = atomic("pay", "pay_order@v1", effect="write_reversible", role="act")
    score = score_planner(case, plan("full", stage(under)), catalog)
    assert scoring.RULE_IRREVERSIBLE_REQUIRES_GATE in codes(score, "rule")
    declared = atomic("pay", "pay_order@v1", effect="write_irreversible", role="act")
    assert scoring.RULE_IRREVERSIBLE_REQUIRES_GATE not in codes(score_planner(case, plan("full", stage(declared)), catalog))
    for ceiling in ("write_reversible", "write_irreversible"):
        loop = goal("book", tools=["flight_search", "card_charge"], effect_ceiling=ceiling)
        assert scoring.RULE_IRREVERSIBLE_REQUIRES_GATE in codes(score_planner(case, plan("full", stage(loop)), catalog)), ceiling
    browse = goal("compare", tools=["flight_search"], effect_ceiling="read")
    assert scoring.RULE_IRREVERSIBLE_REQUIRES_GATE not in codes(score_planner(case, plan("full", stage(browse, declared)), catalog))


def test_a_template_whose_catalog_effect_is_irreversible_counts_even_without_a_label(make_set) -> None:
    case, catalog = load_case(make_set, task_case("t_unlabelled", risk="write_irreversible"))
    under = atomic("pay", "pay_order@v1", effect="write_reversible", role="act")
    assert scoring.RULE_IRREVERSIBLE_REQUIRES_GATE in codes(score_planner(case, plan("full", stage(under)), catalog))


def test_atomic_effects_must_not_be_declared_below_the_catalog(make_set) -> None:
    """写操作按只读声明就不带幂等键、还会被缓存与重试（附录 D.4）。"""
    case, catalog = load_case(make_set, task_case("t_under", risk="write_reversible"))
    under = atomic("save", "save_draft@v1", effect="read", role="fetch")
    assert scoring.RULE_EFFECT_NOT_BELOW_CATALOG in codes(score_planner(case, plan("full", stage(under)), catalog), "rule")
    honest = atomic("save", "save_draft@v1", effect="write_reversible", role="act")
    assert scoring.RULE_EFFECT_NOT_BELOW_CATALOG not in codes(score_planner(case, plan("full", stage(honest)), catalog))


def test_templates_recipes_and_tools_must_exist_in_the_catalog(make_set) -> None:
    case, catalog = load_case(make_set, task_case("t_catalog"))
    invented = atomic("answer", "made_up@v1", executor="model", effect="none", role="deliver")
    assert scoring.RULE_TEMPLATE_IN_CATALOG in codes(score_planner(case, plan("full", stage(invented)), catalog), "rule")
    bad_recipe = goal("batch", mode="template", recipe="unknown_recipe@v1")
    assert scoring.RULE_TEMPLATE_IN_CATALOG in codes(score_planner(case, plan("full", stage(bad_recipe)), catalog), "rule")
    bad_tool = goal("look", tools=["teleport"])
    assert scoring.RULE_TEMPLATE_IN_CATALOG in codes(score_planner(case, plan("full", stage(bad_tool)), catalog), "rule")


def test_effects_must_stay_within_the_ceiling(make_set) -> None:
    case, catalog = load_case(make_set, task_case("t_ceiling", ceiling="read"))
    write = atomic("save", "save_draft@v1", effect="write_reversible", role="act")
    assert scoring.RULE_EFFECT_WITHIN_CEILING in codes(score_planner(case, plan("full", stage(write)), catalog), "rule")
    # 没写 effect_ceiling 的 tool_loop 按 Task 的上限算，不违规；sub_agent 缺省只读。
    assert scoring.RULE_EFFECT_WITHIN_CEILING not in codes(score_planner(case, plan("full", stage(goal("look", tools=["web_search"]))), catalog))


def test_fast_and_light_must_not_call_strong_models(make_set) -> None:
    case, catalog = load_case(make_set, task_case("t_cheap", triage="fast"))
    strong = plan("fast", cost=[usage(0.01, tier="strong@low")])
    assert scoring.RULE_CHEAP_TRIAGE_NO_STRONG_MODEL in codes(score_planner(case, strong, catalog), "rule")
    small = plan("fast", cost=[usage(0.001, tier="small@low")])
    assert codes(score_planner(case, small, catalog), "rule") == set()
    full_case, _ = load_case(make_set, task_case("t_full"))
    assert codes(score_planner(full_case, plan("full", cost=[usage(0.02, tier="strong@medium")]), catalog), "rule") == set()


def test_light_must_hit_a_cached_recipe_that_has_not_failed_recently(make_set) -> None:
    cached = [{"recipe": "refund_analysis@v3", "task_family": "analytics.attribution"}]
    case, catalog = load_case(make_set, task_case("t_light", triage="light", recipe="refund_analysis@v3", recipes=cached))
    hit = plan("light", task_plan={"triage": {"level": "light"}, "horizon": "per_stage", "recipe": "refund_analysis@v3"})
    assert codes(score_planner(case, hit, catalog), "rule") == set()
    miss = plan("light", task_plan={"triage": {"level": "light"}, "horizon": "per_stage", "recipe": "other_recipe@v1"})
    assert scoring.RULE_LIGHT_REQUIRES_RECIPE_HIT in codes(score_planner(case, miss, catalog), "rule")
    failed = [{"recipe": "refund_analysis@v3", "task_family": "analytics.attribution", "recent_failure": True}]
    case2, _ = load_case(make_set, task_case("t_light_failed", triage="full", recipes=failed))
    assert scoring.RULE_LIGHT_REQUIRES_RECIPE_HIT in codes(score_planner(case2, hit, catalog), "rule")


# --- 形状特征 ---


def test_stage_features(make_set) -> None:
    _, catalog = load_case(make_set, task_case("t_features"))
    research = stage(
        atomic("confirm", "confirm_scope@v1", executor="human", effect="none", role="gate"),
        goal("r_market", mode="sub_agent", after=["confirm"]),
        goal("r_tech", mode="sub_agent", after=["confirm"]),
        goal("r_policy", mode="sub_agent", after=["confirm"]),
        atomic("draft", "summarize@v1", executor="model", effect="none", role="transform", after=["r_market", "r_tech", "r_policy"], quality={"type": "revise", "judge": "check_numbers@v1", "max_rounds": 2}),
    )
    features = stage_features(research, catalog)
    assert features["nodes"] == 5
    assert features["fan_out"] == 3
    assert features["gate"] is True and features["human"] is True and features["deliver"] is False
    assert features["modes"] == ["sub_agent"]
    assert features["quality"] == ["revise"]
    assert features["single_writer"] is True
    assert features["node_ids"] == ["confirm", "draft", "r_market", "r_policy", "r_tech"]
    pay = stage(atomic("pay", "pay_order@v1", effect="write_irreversible", role="act"))
    assert stage_features(pay, catalog)["gate"] is True, "write_irreversible 的 Node 前运行时自动插入人工审批"


def test_shape_mismatches_are_reported_per_feature(make_set) -> None:
    shape = {"fan_out": {"min": 3, "max": 5}, "gate": True, "deliver": False, "modes": ["sub_agent"]}
    case, catalog = load_case(make_set, task_case("t_shape", shape=shape, reference_stage=stage(
        atomic("confirm", "confirm_scope@v1", executor="human", effect="none", role="gate"),
        goal("a", mode="sub_agent", after=["confirm"]), goal("b", mode="sub_agent", after=["confirm"]), goal("c", mode="sub_agent", after=["confirm"]),
    )))
    score = score_planner(case, plan("full", answer_stage()), catalog)
    assert {"fan_out", "gate", "deliver", "modes"} <= codes(score, "shape")
    [fan_out] = [f for f in score.failures if f.code == "fan_out"]
    assert "3" in fan_out.message and "1" in fan_out.message


def test_replan_must_not_redo_verified_nodes(make_set) -> None:
    case, catalog = load_case(make_set, task_case("t_keep", triage="replan", shape={"keep_nodes": ["q_region"]}))
    redo = stage(atomic("q_region", "sql_query@v1"), atomic("answer", "answer@v1", executor="model", effect="none", role="deliver", after=["q_region"]))
    assert "keep_nodes" in codes(score_planner(case, plan("replan", redo), catalog), "shape")
    assert "keep_nodes" not in codes(score_planner(case, plan("replan", answer_stage()), catalog))


def test_one_round_plans_only_up_to_the_next_information_checkpoint(make_set) -> None:
    case, catalog = load_case(make_set, task_case("t_horizon", shape={"forbidden_roles": ["deliver"]}, reference_stage=stage(atomic("q", "sql_query@v1"))))
    assert "forbidden_roles" in codes(score_planner(case, plan("full", answer_stage()), catalog), "shape")


def test_writers_must_not_run_in_parallel_when_single_writer_is_required(make_set) -> None:
    case, catalog = load_case(make_set, task_case("t_writer", risk="write_reversible", shape={"single_writer": True}))
    parallel = stage(goal("fix_a", tools=["repo_edit"], effect_ceiling="write_reversible"), goal("fix_b", tools=["repo_edit"], effect_ceiling="write_reversible"))
    assert "single_writer" in codes(score_planner(case, plan("full", parallel), catalog), "shape")


# --- 成本与说明文字 ---


def test_cost_and_vendors_come_from_usage(make_set) -> None:
    case, catalog = load_case(make_set, task_case("t_cost"))
    score = score_planner(case, plan("full", cost=[usage(0.02, vendor="vendor_a"), usage(0.01, vendor="vendor_b")]), catalog)
    assert score.cost_usd == pytest.approx(0.03)
    assert score.vendors == frozenset({"vendor_a", "vendor_b"})


def test_failure_messages_do_not_quote_the_case(make_set) -> None:
    """留出集的报告会汇总失败原因，说明文字里不能带用例内容。"""
    message = "帮我订下周二去上海最便宜的机票并付款"
    case, catalog = load_case(make_set, task_case("t_quote", triage="fast", message=message, shape={"gate": True}))
    score = score_planner(case, plan("full", stage(goal("do", goal=message))), catalog)
    assert score.failures
    assert all(message not in f.message and "t_quote" not in f.message for f in score.failures)


# --- Reviewer ---


def test_the_reference_verdict_passes(make_set) -> None:
    sample = load_sample(make_set, reviewer_sample("r_ok"))
    score = score_reviewer(sample.checkpoint, ReviewerOutput(verdict=verdict("done")))
    assert score.passed and score.failures == []


def test_a_verdict_outside_the_acceptable_range_fails(make_set) -> None:
    sample = load_sample(make_set, reviewer_sample("r_range", name="replan", acceptable=["replan", "escalate"], min_escalation="replan"))
    assert score_reviewer(sample.checkpoint, ReviewerOutput(verdict=verdict("escalate"))).passed
    score = score_reviewer(sample.checkpoint, ReviewerOutput(verdict=verdict("abort")))
    assert not score.passed and "verdict" in {f.dimension for f in score.failures}
    assert not score.failed("escalation"), "abort 高于 min_escalation，不算原地重试"


def test_an_invalid_verdict_fails_the_schema_check(make_set) -> None:
    sample = load_sample(make_set, reviewer_sample("r_schema", name="repair", min_escalation="repair_retry"))
    score = score_reviewer(sample.checkpoint, ReviewerOutput(verdict={"verdict": "repair", "reason": "缺 repair 细节"}))
    assert not score.passed and "verdict_schema" in codes(score, "schema")


def test_repairing_below_the_required_escalation_counts_as_stuck_retry(make_set) -> None:
    history = [{"round": 1, "verdict": "repair", "failed_nodes": {"answer": "TimeoutError"}}, {"round": 2, "verdict": "repair", "failed_nodes": {"answer": "TimeoutError"}}]
    failing = {"answer": {"role": "deliver", "status": "failed", "failure_class": "invalid_output", "error": "TimeoutError", "attempts": 3}}
    sample = load_sample(make_set, reviewer_sample("r_stuck", name="replan", min_escalation="replan", history=history, results=failing, symptom="revise_same_error"))
    score = score_reviewer(sample.checkpoint, ReviewerOutput(verdict=verdict("repair")))
    assert "stuck_retry" in codes(score, "escalation")
    changed = verdict("repair", repair={"nodes": ["answer"], "strategy": "change_method"})
    assert "stuck_retry" in codes(score_reviewer(sample.checkpoint, ReviewerOutput(verdict=changed)), "escalation")
    assert not score_reviewer(sample.checkpoint, ReviewerOutput(verdict=verdict("replan"))).failures


def test_done_must_name_a_succeeded_deliver_node(make_set) -> None:
    results = {"answer": {"role": "deliver", "status": "failed", "failure_class": "invalid_output"}}
    sample = load_sample(make_set, reviewer_sample("r_done_failed", name="repair", min_escalation="repair_retry", results=results))
    score = score_reviewer(sample.checkpoint, ReviewerOutput(verdict=verdict("done")))
    assert scoring.RULE_DONE_FROM_DELIVER in codes(score, "rule")
    ok = load_sample(make_set, reviewer_sample("r_done_ok"))
    wrong_node = verdict("done", done={"deliver_node": "nowhere"})
    assert scoring.RULE_DONE_FROM_DELIVER in codes(score_reviewer(ok.checkpoint, ReviewerOutput(verdict=wrong_node)), "rule")


@pytest.mark.parametrize(
    ("value", "step"),
    [
        (verdict("done"), "none"),
        (verdict("continue"), "none"),
        (verdict("repair"), "repair_retry"),
        (verdict("repair", repair={"nodes": ["answer"], "strategy": "change_method"}), "repair_change_method"),
        (verdict("replan"), "replan"),
        (verdict("replan", replan={"change_shape": True}), "replan_change_shape"),
        (verdict("reroute"), "replan_change_shape"),
        (verdict("rollback"), "rollback"),
        (verdict("escalate"), "escalate"),
        (verdict("abort"), "abort"),
    ],
)
def test_escalation_steps_follow_appendix_b8(value: dict[str, Any], step: str) -> None:
    assert escalation_step(value) == step
    assert scoring.ESCALATION_ORDER.index(step) >= 0


def test_reviewer_cost_comes_from_usage(make_set) -> None:
    sample = load_sample(make_set, reviewer_sample("r_cost"))
    score = score_reviewer(sample.checkpoint, ReviewerOutput(verdict=verdict("done"), usage=[usage(0.004, vendor="vendor_c")]))
    assert score.cost_usd == pytest.approx(0.004) and score.vendors == frozenset({"vendor_c"})


def test_task_state_fixture_helper_is_schema_valid(make_set) -> None:
    """辅助函数自检：带 Round 记录的 Task State 能通过加载。"""
    state = task_state(rounds=[{"seq": 1, "stage_goal": "取数", "status": "completed", "verdict": "continue"}])
    load_eval_set(make_set([task_case("t_state", rounds=[checkpoint(state=state)])]))
