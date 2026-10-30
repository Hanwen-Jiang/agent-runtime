"""S-10 验收：规则先于模型（附录 B.8；计划 S-10 难点“规则先于模型”“防震荡”“完成看产出物”“问人”）。

floor 由代码从台账算出“至少要升级到哪一步”；enforce 拿模型的提议（或没有提议）对照 floor 与各级次数上限，
只会往上调，不会往下调，并把细节补成合法的 Verdict。这里不调用模型：提议直接给出。
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from agentruntime_worker.reviewer.rules import Caps, Decision, Floor, ReviewCatalog, Thresholds, enforce, floor
from agentruntime_worker.reviewer.signals import STEPS, Ledger, ledger, step_of

from .reviewer_support import (
    CATALOG,
    HUMAN_REPLIES,
    abort,
    atomic,
    change_method,
    cont,
    done,
    escalate_human,
    escalate_tier,
    failed,
    goal,
    ok,
    past,
    replan,
    retry,
    rounds,
    skipped,
    stage,
    task_state,
)
from .support import assert_valid

LINT_STAGE = stage(goal("fix_lint"), atomic("lint", role="judge", template="run_tests@v1", executor="program", effect="read", after=("fix_lint",)))
LINT_FAIL = {"fix_lint": ok(), "lint": failed("flake8: E501", role="judge")}


def lint_history(times: int) -> list[dict[str, Any]]:
    """lint 在之前的 times 个 Round 里都失败了。"""
    steps = ["repair_retry", "repair_change_method", "replan"]
    return [past(i, "repair", step=steps[min(i - 1, 1)], failed_nodes={"lint": "flake8: E501"}) for i in range(1, times + 1)]


def decide(
    stage_: dict[str, Any],
    results: dict[str, Any],
    proposal: dict[str, Any] | None,
    *,
    history: list[dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
    completion: float | None = 0.5,
    catalog: ReviewCatalog | None = CATALOG,
    caps: Caps = Caps(),
) -> tuple[Decision, Floor, Ledger]:
    history = history or []
    state = state if state is not None else task_state(rounds=rounds(*[h["verdict"] for h in history]))
    lg = ledger(state, stage_, results, history)
    fl = floor(lg, results, completion=completion, thresholds=Thresholds())
    decision = enforce(None if proposal is None else proposal["verdict"], floor=fl, ledger=lg, stage=stage_, results=results, catalog=catalog, caps=caps)
    assert_valid("verdict", decision.verdict)
    return decision, fl, lg


# --- 同一 Node 失败 2 次换方法，3 次 replan ---


def test_a_first_failure_may_be_retried_in_place() -> None:
    d, fl, _ = decide(LINT_STAGE, LINT_FAIL, retry("fix_lint"))
    assert fl.step == "repair_retry"
    assert d.verdict["repair"] == {"nodes": ["fix_lint"], "strategy": "retry_with_error"} and not d.raised


def test_the_second_failure_of_the_same_node_requires_changing_method() -> None:
    d, fl, _ = decide(LINT_STAGE, LINT_FAIL, retry("fix_lint"), history=lint_history(1))
    assert fl.step == "repair_change_method"
    assert d.verdict["verdict"] == "repair" and d.verdict["repair"]["strategy"] == "change_method"
    assert d.raised and any("2" in r for r in d.reasons), d.reasons


def test_the_third_failure_requires_replan() -> None:
    d, fl, _ = decide(LINT_STAGE, LINT_FAIL, retry("fix_lint"), history=lint_history(2))
    assert fl.step == "replan"
    assert d.verdict["verdict"] == "replan" and d.raised


def test_a_denied_action_is_never_retried_in_place() -> None:
    s = stage(atomic("list", role="fetch"), goal("mark", mode="template", recipe="ticket_mark@v1", after=("list",)))
    results = {"list": ok("fetch"), "mark": failed("租户策略只允许只读", failure_class="denied")}
    d, fl, _ = decide(s, results, retry("mark"))
    assert fl.step == "replan"
    assert d.verdict["verdict"] == "replan"
    kept, _, _ = decide(s, results, escalate_human("要申请写权限吗？"))
    assert kept.verdict["verdict"] == "escalate" and not kept.raised, "问人要权限高于 floor，照模型的判断"


def test_any_failed_node_rules_out_continue_and_done() -> None:
    """有 Node 失败时，本 Stage 的退出条件不算满足：continue 与 done 都被抬到失败的 floor。"""
    s = stage(atomic("synthesize"), atomic("check_cites", role="judge", after=("synthesize",)), atomic("report", role="deliver", after=("check_cites",)))
    results = {"synthesize": ok(), "check_cites": failed("3 处引用与原文不符", role="judge"), "report": skipped("check_cites", role="deliver")}
    for proposal in (cont(), done("report")):
        d, _, _ = decide(s, results, proposal)
        assert d.verdict["verdict"] == "repair" and d.raised


# --- 附录 B.6、B.7 里能由代码算出的几条（2026-10-28 真模型实验第 3.2 条：模型在这些情形下原地重试或假完成） ---

TAX_ERROR = "ImportError: cannot import name 'TaxTable'"
REVISE_STAGE = stage(goal("fix", quality={"type": "revise", "judge": "run_tests@v1", "max_rounds": 3}), atomic("report", role="deliver", after=("fix",)))


@pytest.mark.parametrize(
    "retried",
    [{"quality": {"type": "revise", "rounds": 3, "passed": False}}, {"attempts": 3}],
    ids=["revise_rounds", "attempts"],
)
def test_the_same_error_again_after_retrying_inside_the_round_requires_replan(retried: dict[str, Any]) -> None:
    """附录 B.7：revise 循环里同一错误反复出现，问题在上游，停止修复，升级为 replan（B.3：同一错误连续 3 次时判定 replan）。
    本轮 Node 自己已重试过（revise 的轮数，或 S-08 样本的 attempts），上一轮又是同一个错误签名。"""
    results = {"fix": {**failed(TAX_ERROR), **retried}, "report": skipped("fix", role="deliver")}
    history = [past(1, "repair", failed_nodes={"fix": TAX_ERROR})]
    d, fl, lg = decide(REVISE_STAGE, results, retry("fix"), history=history)
    assert "repetition" in lg.signals
    assert fl.step == "replan" and d.verdict["verdict"] == "replan" and d.raised, d.reasons
    assert any("B.7" in r for r in fl.reasons)


def test_a_wrapped_error_is_still_the_same_error() -> None:
    """质量包装失败时错误前面带说明（S-08 的 rs_revise_same_error 就是这样）：包含上一轮的错误签名也算同一个错误；太短的签名不按包含算。"""
    results = {"fix": {**failed(f"revise 三轮都卡在同一错误：{TAX_ERROR}"), "attempts": 3}, "report": skipped("fix", role="deliver")}
    _, fl, lg = decide(REVISE_STAGE, results, retry("fix"), history=[past(1, "repair", failed_nodes={"fix": TAX_ERROR})])
    assert lg.repeated == ("fix",) and fl.step == "replan"
    short = {"fix": {**failed("timeout after retry"), "attempts": 3}, "report": skipped("fix", role="deliver")}
    _, _, lg2 = decide(REVISE_STAGE, short, retry("fix"), history=[past(1, "repair", failed_nodes={"fix": "timeout"})])
    assert lg2.repeated == ()


def test_a_repeated_error_without_retries_inside_the_round_only_changes_method() -> None:
    """同一错误第 2 次、但这一轮没在 Node 里重试过：仍按 B.8 换方法（不越级到 replan）。"""
    results = {"fix": failed(TAX_ERROR), "report": skipped("fix", role="deliver")}
    _, fl, _ = decide(REVISE_STAGE, results, retry("fix"), history=[past(1, "repair", failed_nodes={"fix": TAX_ERROR})])
    assert fl.step == "repair_change_method"


def test_a_different_error_after_retries_is_not_a_repetition() -> None:
    results = {"fix": {**failed("AssertionError: 3 != 2"), "attempts": 3}, "report": skipped("fix", role="deliver")}
    _, fl, lg = decide(REVISE_STAGE, results, retry("fix"), history=[past(1, "repair", failed_nodes={"fix": TAX_ERROR})])
    assert "repetition" not in lg.signals and fl.step == "repair_change_method"


DRAFT_STAGE = stage(
    atomic("draft", template="synthesize_report@v1", quality={"type": "revise", "judge": "rubric_judge@v1", "max_rounds": 3}),
    atomic("report", role="deliver", after=("draft",)),
)
UNPASSED = {"draft": {**ok(), "quality": {"type": "revise", "rounds": 3, "passed": False}}, "report": ok("deliver")}


@pytest.mark.parametrize("proposal", [done("report"), cont()], ids=["done", "continue"])
def test_a_quality_loop_that_never_passed_its_judge_is_not_done(proposal: dict[str, Any]) -> None:
    """Stage Runner 把用满 max_rounds 仍没通过 judge 的 revise 记成 succeeded、quality.passed = false（stage_runner._revise）。
    产出没有经过验证：不能判 done 或 continue；revise 已经原地重试过 max_rounds 轮，换方法（附录 B.7：引入外部判据或独立 judge）。
    E4 里“做不到却判 done”与 S-08 的 rs_cosmetic_reflection 都是这一类。"""
    d, fl, _ = decide(DRAFT_STAGE, UNPASSED, proposal)
    assert fl.step == "repair_change_method"
    assert d.verdict["verdict"] == "repair" and d.verdict["repair"]["nodes"] == ["draft"] and d.raised, d.verdict


def test_a_quality_loop_that_passed_may_be_done() -> None:
    passed = {"draft": {**ok(), "quality": {"type": "revise", "rounds": 2, "passed": True}}, "report": ok("deliver")}
    d, fl, _ = decide(DRAFT_STAGE, passed, done("report"))
    assert fl.step == "none" and d.verdict["verdict"] == "done" and not d.raised


FANOUT_STAGE = stage(
    goal("refactor_api"), goal("refactor_db"), goal("refactor_ui"),
    atomic("merge", role="aggregate", template="summarize@v1", after=("refactor_api", "refactor_db", "refactor_ui")),
)
MERGE_FAILED = {"refactor_api": ok(), "refactor_db": ok(), "refactor_ui": ok(), "merge": failed("三个分支都改了 config.py 的同一段，无法合并", role="aggregate")}


def test_a_merge_that_fails_after_fan_out_requires_replan() -> None:
    """附录 B.7：扇出后子结果冲突、合并困难，是耦合度被低估：收回单一写者，或先定共享接口再扇出。冲突的分支不保留。"""
    d, fl, lg = decide(FANOUT_STAGE, MERGE_FAILED, retry("merge"))
    assert "parallel_anomaly" in lg.signals
    assert fl.step == "replan" and d.verdict["verdict"] == "replan" and d.raised
    assert not set(d.verdict["replan"].get("keep_nodes") or []) & {"refactor_api", "refactor_db", "refactor_ui"}


def test_a_failed_aggregate_over_one_input_is_an_ordinary_failure() -> None:
    s = stage(goal("collect"), atomic("merge", role="aggregate", template="summarize@v1", after=("collect",)))
    _, fl, lg = decide(s, {"collect": ok(), "merge": failed("格式不对", role="aggregate")}, retry("merge"))
    assert "parallel_anomaly" not in lg.signals and fl.step == "repair_retry"


def test_done_that_contradicts_its_own_progress_check_is_not_accepted() -> None:
    """Progress Check 的第一个问题是“是否已满足验收条件”（附录 B.6）；模型自己答了没满足，却判 done，代码不收。"""
    s = stage(atomic("fix"), atomic("report", role="deliver", after=("fix",)))
    results = {"fix": ok(), "report": ok("deliver")}
    contradictory = done("report")
    contradictory["verdict"]["progress_check"] = {"criteria_met": False, "progress": True, "looping": False, "assumptions_hold": True, "next_step": "再跑一遍测试"}
    d, _, _ = decide(s, results, contradictory)
    assert d.verdict["verdict"] == "continue" and d.raised
    assert any("Progress Check" in r for r in d.reasons)


# --- 换形状与预算 ---


def test_two_replans_without_progress_require_a_new_shape() -> None:
    s = stage(atomic("fetch_spec", role="fetch"), atomic("digest", after=("fetch_spec",)))
    results = {"fetch_spec": failed("规格页不存在", role="fetch"), "digest": skipped("fetch_spec")}
    history = [past(1, "replan", failed_nodes={"fetch_old": "下线"}), past(2, "replan", failed_nodes={"fetch_guess": "不存在"})]
    d, fl, _ = decide(s, results, replan(), history=history)
    assert fl.step == "replan_change_shape"
    assert d.verdict["verdict"] == "replan" and d.verdict["replan"]["change_shape"] is True
    reroute = {"verdict": {"verdict": "reroute", "reason": "换流程", "reroute": {"recipe": "ticket_triage@v4"}}}
    kept, _, _ = decide(s, results, reroute, history=history)
    assert kept.verdict["verdict"] == "reroute" and not kept.raised


def test_burning_80_percent_of_the_budget_with_under_half_done_narrows_scope_or_asks() -> None:
    state = task_state(rounds=rounds("continue", costs=[0.5], current_cost=0.35), budget=1.0)
    s = stage(goal("rate_batch2", mode="sub_agent"))
    results = {"rate_batch2": ok(result={"rated_total": 20, "total": 50})}
    d, fl, _ = decide(s, results, cont(completion=0.3), state=state, completion=0.3)
    assert fl.allowed == frozenset({"replan", "escalate", "abort"})
    assert "budget_burn" in fl.signals and "budget_burn" in d.verdict["signals"]
    assert d.verdict["verdict"] == "escalate" and d.verdict["escalate"]["target"] == "human" and d.raised
    narrowed, _, _ = decide(s, results, replan(stuck="只做前 30 家"), state=state, completion=0.3)
    assert narrowed.verdict["verdict"] == "replan" and not narrowed.raised, "缩小范围的 replan 是允许的"
    healthy, fl2, _ = decide(s, results, cont(completion=0.7), state=state, completion=0.7)
    assert fl2.allowed is None and healthy.verdict["verdict"] == "continue"


def test_a_spent_budget_leaves_only_asking_or_stopping() -> None:
    state = task_state(rounds=rounds("continue", "continue", costs=[0.9, 0.8], current_cost=0.35), budget=2.0)
    s = stage(goal("rate_batch3", mode="sub_agent"))
    results = {"rate_batch3": ok(result={"rated_total": 31, "total": 50})}
    d, fl, _ = decide(s, results, replan(), state=state, completion=0.62)
    assert fl.allowed == frozenset({"escalate", "abort"})
    assert d.verdict["verdict"] == "escalate" and d.raised
    stop, _, _ = decide(s, results, abort(["31 家已评级"], ["其余 19 家"]), state=state, completion=0.62)
    assert stop.verdict["verdict"] == "abort" and not stop.raised


def test_the_budget_rule_needs_a_budget() -> None:
    _, fl, _ = decide(stage(atomic("a")), {"a": ok()}, cont(completion=0.1), completion=0.1)
    assert fl.allowed is None


# --- 次数上限（防震荡） ---


@pytest.mark.parametrize(
    ("history", "proposal", "expected"),
    [
        # 上次进展之后已原地重试 2 次 → 不再原地重试
        ([past(1, "repair", step="repair_retry", failed_nodes={"x": "e"}), past(2, "repair", step="repair_retry", failed_nodes={"y": "e"})], retry("a"), "repair_change_method"),
        # 换方法也用完 → replan
        (
            [past(i, "repair", step=s, failed_nodes={f"n{i}": "e"}) for i, s in enumerate(["repair_retry", "repair_retry", "repair_change_method", "repair_change_method"], start=1)],
            retry("a"),
            "replan",
        ),
        # 换形状（含 reroute）整个 Task 只有一次 → escalate
        ([past(1, "replan", step="replan_change_shape", failed_nodes={"x": "e"})], replan(change_shape=True), "escalate"),
        # rollback 整个 Task 只有一次 → escalate
        ([past(1, "continue", step="none"), past(2, "rollback", step="rollback", failed_nodes={"x": "e"})], {"verdict": {"verdict": "rollback", "reason": "又改坏了", "rollback": {"checkpoint": "round_1"}}}, "escalate"),
    ],
)
def test_a_step_whose_cap_is_used_up_moves_to_the_next_rung(history: list[dict[str, Any]], proposal: dict[str, Any], expected: str) -> None:
    d, _, _ = decide(stage(atomic("a")), {"a": failed("e")}, proposal, history=history)
    assert step_of(d.verdict) == expected and d.raised


def test_escalation_tries_a_stronger_tier_once_then_asks_a_human_twice_then_aborts() -> None:
    shape_used = past(1, "replan", step="replan_change_shape", failed_nodes={"x": "e"})
    tier_used = past(2, "escalate", step="escalate", escalate="tier", failed_nodes={"x": "e"})
    asked = [past(3, "escalate", step="escalate", escalate="human"), past(4, "escalate", step="escalate", escalate="human")]
    first, _, _ = decide(stage(atomic("a")), {"a": failed("e")}, replan(change_shape=True), history=[shape_used])
    assert first.verdict["escalate"]["target"] == "tier" and first.verdict["escalate"]["tier"]
    second, _, _ = decide(stage(atomic("a")), {"a": failed("e")}, escalate_tier(), history=[shape_used, tier_used])
    assert second.verdict["escalate"]["target"] == "human"
    last, _, _ = decide(stage(atomic("a")), {"a": failed("e")}, escalate_human(), history=[shape_used, tier_used, *asked])
    assert last.verdict["verdict"] == "abort"
    assert last.verdict["abort"]["not_completed"], "abort 要说清没完成什么"
    assert last.verdict["reason_code"], "规则写的 abort 带代码，客户端才翻得了它的交付"


def test_the_model_may_choose_a_higher_step_than_the_floor() -> None:
    d, fl, _ = decide(LINT_STAGE, LINT_FAIL, escalate_human("lint 规则是否可以放宽？"))
    assert fl.step == "repair_retry"
    assert d.verdict["verdict"] == "escalate" and not d.raised


# --- 细节的合法性 ---


def test_done_needs_a_deliver_node_that_succeeded_in_this_stage() -> None:
    s = stage(atomic("market"), atomic("draft", after=("market",)), exit_when="草稿完成")
    no_deliver, _, _ = decide(s, {"market": ok(), "draft": ok()}, done("draft"))
    assert no_deliver.verdict["verdict"] == "continue" and no_deliver.raised, "没有 deliver 节点的 Round 不能判 done"
    s2 = stage(atomic("synthesize"), atomic("report", role="deliver", after=("synthesize",)))
    corrected, _, _ = decide(s2, {"synthesize": ok(), "report": ok("deliver", {"pages": 6})}, done("synthesize"))
    assert corrected.verdict["done"] == {"deliver_node": "report"}


def test_a_human_escalation_always_offers_exactly_the_three_replies() -> None:
    d, _, _ = decide(LINT_STAGE, LINT_FAIL, escalate_human("放宽规则吗？", options=["continue_as_is"]))
    esc = d.verdict["escalate"]
    assert esc["options"] == HUMAN_REPLIES
    assert esc["question"] == "放宽规则吗？" and esc["state_summary"]
    # 代码自己抬到问人时，也要给出具体的问题、状态摘要与三种回复。
    raised, _, _ = decide(LINT_STAGE, LINT_FAIL, retry("fix_lint"), history=lint_history(2), caps=Caps(replan=0, replan_change_shape=0, escalate_tier=0))
    assert raised.verdict["verdict"] == "escalate" and raised.raised
    esc = raised.verdict["escalate"]
    assert esc["target"] == "human" and esc["options"] == HUMAN_REPLIES and esc["question"] and esc["state_summary"]


def test_rollback_only_to_a_known_checkpoint() -> None:
    history = [past(1, "continue"), past(2, "continue")]
    proposal = {"verdict": {"verdict": "rollback", "reason": "账户被清空", "rollback": {"checkpoint": "round_2"}}}
    d, _, lg = decide(stage(atomic("e2e", role="judge")), {"e2e": failed("accounts.db 记录数为 0", role="judge")}, proposal, history=history)
    assert lg.checkpoints == ("round_1", "round_2")
    assert d.verdict["verdict"] == "rollback" and not d.raised
    unknown = {"verdict": {"verdict": "rollback", "reason": "账户被清空", "rollback": {"checkpoint": "commit_round_9"}}}
    raised, _, _ = decide(stage(atomic("e2e", role="judge")), {"e2e": failed("accounts.db 记录数为 0", role="judge")}, unknown, history=history)
    assert raised.verdict["verdict"] == "escalate" and raised.raised, "没有这个检查点就退不回去，往上一级"


def test_reroute_only_to_a_recipe_in_the_catalog_within_the_effect_ceiling() -> None:
    s = stage(goal("explore"))
    results = {"explore": ok(result={"finding": "适合工单批处理流程"})}

    def reroute(recipe: str) -> dict[str, Any]:
        return {"verdict": {"verdict": "reroute", "reason": "任务属于另一类流程", "reroute": {"recipe": recipe}}}

    kept, _, _ = decide(s, results, reroute("ticket_triage@v4"))
    assert kept.verdict["verdict"] == "reroute" and kept.verdict["reroute"] == {"recipe": "ticket_triage@v4"} and not kept.raised
    unknown, _, _ = decide(s, results, reroute("made_up@v1"))
    assert unknown.verdict["verdict"] == "replan" and unknown.verdict["replan"]["change_shape"] is True, "编造的 Recipe 不能用：同级的换形状"
    too_risky, _, _ = decide(s, results, reroute("flight_booking@v1"))
    assert too_risky.verdict["verdict"] == "escalate" and too_risky.verdict["escalate"]["target"] == "human", "超出 effect 上限的流程要先问人授权"


def test_repair_nodes_and_keep_nodes_must_be_nodes_of_this_stage() -> None:
    d, _, _ = decide(LINT_STAGE, LINT_FAIL, retry("ghost"))
    assert d.verdict["repair"]["nodes"] == ["fix_lint"], "不存在的 Node 由代码重选：失败的是 judge 时修它检查的上游产出"
    s0 = stage(atomic("q_a", role="fetch"), atomic("q_b", role="fetch"))
    d0, _, _ = decide(s0, {"q_a": ok("fetch"), "q_b": failed("rate_limit", role="fetch", failure_class="transient")}, retry("ghost"))
    assert d0.verdict["repair"]["nodes"] == ["q_b"], "失败的不是 judge 时修它自己"
    s = stage(goal("read_tickets", mode="sub_agent"), atomic("analyze", template="refund_attribution@v2", after=("read_tickets",)))
    results = {"read_tickets": ok(result={"empty_reason_share": 0.62}), "analyze": failed("62% 的原因字段为空")}
    kept, _, _ = decide(s, results, replan(keep=["read_tickets", "analyze", "ghost"]))
    assert kept.verdict["replan"]["keep_nodes"] == ["read_tickets"], "只能保留本轮成功的 Node"


def test_raised_replan_keeps_succeeded_nodes_but_not_the_producer_a_judge_rejected() -> None:
    """代码把 repair 抬到 replan 时：保留成功的 Node，但被 judge 否掉的上游产出不保留（它正是要重做的）。"""
    d, _, _ = decide(LINT_STAGE, LINT_FAIL, retry("fix_lint"), history=lint_history(2))
    assert d.verdict["verdict"] == "replan" and d.verdict["replan"].get("keep_nodes", []) == []
    s = stage(goal("read_tickets", mode="sub_agent"), atomic("analyze", after=("read_tickets",)))
    results = {"read_tickets": ok(), "analyze": failed("无法归因")}
    history = [past(1, "repair", failed_nodes={"analyze": "无法归因"}), past(2, "repair", step="repair_change_method", failed_nodes={"analyze": "无法归因"})]
    d2, _, _ = decide(s, results, retry("analyze"), history=history)
    assert d2.verdict["verdict"] == "replan" and d2.verdict["replan"]["keep_nodes"] == ["read_tickets"]


PROPOSALS = [None, retry("a"), change_method("a"), replan(), replan(change_shape=True), escalate_human(), escalate_tier(), cont(), done("a"), abort()]
SCENARIOS = [
    ({"a": ok("deliver")}, []),
    ({"a": failed("e", role="deliver")}, []),
    ({"a": failed("e", role="deliver")}, [past(1, "repair", failed_nodes={"a": "e"})]),
    ({"a": failed("e", role="deliver", failure_class="denied")}, [past(1, "replan", failed_nodes={"b": "e"}), past(2, "replan", failed_nodes={"c": "e"})]),
    ({"a": failed("e", role="deliver")}, [past(1, "escalate", step="escalate", escalate="human"), past(2, "escalate", step="escalate", escalate="human"), past(3, "replan", step="replan_change_shape")]),
]


@pytest.mark.parametrize(("proposal", "scenario"), list(itertools.product(PROPOSALS, SCENARIOS)))
def test_every_enforced_verdict_matches_the_schema_and_never_goes_below_the_floor(proposal: dict[str, Any] | None, scenario: tuple[dict[str, Any], list[dict[str, Any]]]) -> None:
    results, history = scenario
    s = stage(atomic("a", role="deliver"))
    d, fl, _ = decide(s, results, proposal, history=history)  # decide 里已校验 verdict.schema.json
    if fl.allowed is None:
        assert STEPS.index(step_of(d.verdict)) >= STEPS.index(fl.step) or d.verdict["verdict"] == "abort"
    else:
        assert d.verdict["verdict"] in fl.allowed
