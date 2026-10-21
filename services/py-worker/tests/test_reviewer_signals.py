"""S-10 验收：硬信号与升级台账由代码计算（附录 B.6、B.8；计划 S-10 交付“硬信号计算”）。

台账从 Task State、本轮的 Stage 与结果、之前各 Round 的摘要（history）算出：每个 Node 失败过几个 Round、
上次进展之后各级升级用了几次、整个 Task 里换形状、rollback、escalate 各用了几次、已花的预算、可退回的检查点。
"""

from __future__ import annotations

import pytest

from agentruntime_worker.reviewer.signals import (
    HARD_SIGNALS,
    STEPS,
    error_signature,
    failed_nodes,
    history_entry,
    ledger,
    step_of,
)

from .reviewer_support import atomic, failed, goal, ok, past, rounds, skipped, stage, task_state

LINT_STAGE = stage(goal("fix_lint"), atomic("lint", role="judge", template="run_tests@v1", executor="program", effect="read", after=("fix_lint",)))


# --- Node 的失败 ---


def test_a_node_failure_counts_once_for_every_round_it_failed_in() -> None:
    history = [past(1, "repair", failed_nodes={"lint": "flake8: E501 line too long"})]
    results = {"fix_lint": ok(), "lint": failed("flake8: E501 line too long", role="judge")}
    lg = ledger(task_state(rounds=rounds("repair")), LINT_STAGE, results, history)
    assert dict(lg.node_failures) == {"lint": 2}


def test_skipped_downstream_nodes_and_recovered_retries_do_not_count_as_failures() -> None:
    s = stage(atomic("q_a", role="fetch"), atomic("q_b", role="fetch"), atomic("report", role="deliver", after=("q_a", "q_b")))
    results = {"q_a": ok("fetch"), "q_b": failed("rate_limit", role="fetch", failure_class="transient"), "report": skipped("q_b", role="deliver")}
    lg = ledger(task_state(), s, results, [])
    assert dict(lg.node_failures) == {"q_b": 1}, "Temporal 重试后成功的不算失败；上游失败而跳过的也不算"


def test_unknown_results_count_as_failures() -> None:
    """写操作结果未知（附录 K.4）也要交给升级规则，不能当作成功。"""
    results = {"pay": failed("超时，结果未知", role="act", failure_class="transient", status="unknown")}
    assert set(failed_nodes(results)) == {"pay"}


def test_failed_nodes_map_ids_to_error_signatures() -> None:
    results = {"a": ok(), "b": failed("E501 at line 12"), "c": skipped("b")}
    assert failed_nodes(results) == {"b": error_signature("E501 at line 12")}


def test_error_signatures_ignore_numbers_and_spacing() -> None:
    """同一个错误换了行号、计数或空白，仍是同一个签名（重复信号据此判断）。"""
    assert error_signature("E501 at line 12 ") == error_signature("E501  at line 40")
    assert error_signature("ImportError: TaxTable") != error_signature("KeyError: TaxTable")
    assert len(error_signature("x" * 10_000)) <= 200


# --- 升级的步骤与历史 ---


@pytest.mark.parametrize(
    ("verdict", "step"),
    [
        ({"verdict": "done", "done": {"deliver_node": "a"}}, "none"),
        ({"verdict": "continue"}, "none"),
        ({"verdict": "repair", "repair": {"nodes": ["a"], "strategy": "retry_with_error"}}, "repair_retry"),
        ({"verdict": "repair", "repair": {"nodes": ["a"], "strategy": "change_method"}}, "repair_change_method"),
        ({"verdict": "replan"}, "replan"),
        ({"verdict": "replan", "replan": {"change_shape": True}}, "replan_change_shape"),
        ({"verdict": "reroute", "reroute": {"recipe": "ticket_triage@v4"}}, "replan_change_shape"),
        ({"verdict": "rollback", "rollback": {"checkpoint": "round_1"}}, "rollback"),
        ({"verdict": "escalate", "escalate": {"target": "tier", "tier": "strong@high"}}, "escalate"),
        ({"verdict": "abort", "abort": {"completed": [], "not_completed": ["x"]}}, "abort"),
    ],
)
def test_each_verdict_maps_to_its_step_on_the_b8_ladder(verdict: dict, step: str) -> None:
    assert step_of(verdict) == step
    assert step in STEPS


def test_history_entry_summarizes_a_round_for_the_next_review() -> None:
    results = {"fix_lint": ok(), "lint": failed("E501", role="judge")}
    verdict = {"verdict": "repair", "reason": "换方法", "repair": {"nodes": ["fix_lint"], "strategy": "change_method"}}
    assert history_entry(2, verdict, results) == {
        "round": 2,
        "verdict": "repair",
        "step": "repair_change_method",
        "failed_nodes": {"lint": error_signature("E501")},
    }
    human = {"verdict": "escalate", "reason": "问人", "escalate": {"target": "human", "question": "继续吗？", "options": ["continue_as_is"]}}
    assert history_entry(3, human, {"a": ok()}) == {"round": 3, "verdict": "escalate", "step": "escalate", "escalate": "human"}


def test_ladder_counts_restart_after_progress_but_task_wide_steps_do_not() -> None:
    history = [
        past(1, "repair", step="repair_retry", failed_nodes={"a": "x"}),
        past(2, "replan", step="replan_change_shape", failed_nodes={"a": "x"}),
        past(3, "escalate", step="escalate", escalate="tier", failed_nodes={"b": "y"}),
        past(4, "continue", step="none"),
        past(5, "repair", step="repair_retry", failed_nodes={"c": "z"}),
        past(6, "repair", step="repair_change_method", failed_nodes={"c": "z"}),
        past(7, "replan", step="replan", failed_nodes={"c": "z"}),
    ]
    lg = ledger(task_state(rounds=rounds(*[h["verdict"] for h in history])), stage(atomic("d")), {"d": failed("w")}, history)
    assert dict(lg.since_progress) == {"repair_retry": 1, "repair_change_method": 1, "replan": 1}
    assert dict(lg.per_task) == {"replan_change_shape": 1, "rollback": 0, "escalate_tier": 1, "escalate_human": 0}
    assert lg.rounds_without_progress == 3


def test_history_without_steps_uses_each_verdicts_default_step() -> None:
    """S-08 的样本只记 Verdict：repair 按原地重试算，replan 按不换形状算，escalate 按问人算。"""
    history = [past(1, "repair"), past(2, "replan"), past(3, "escalate")]
    lg = ledger(task_state(rounds=rounds("repair", "replan", "escalate")), stage(atomic("a")), {"a": failed("x")}, history)
    assert lg.since_progress["repair_retry"] == 1 and lg.since_progress["replan"] == 1
    assert lg.per_task["escalate_human"] == 1 and lg.per_task["escalate_tier"] == 0


# --- 预算与检查点 ---


def test_spent_budget_adds_up_the_round_costs_in_the_task_state() -> None:
    state = task_state(rounds=rounds("continue", "continue", costs=[0.9, 0.8], current_cost=0.35), budget=2.0)
    lg = ledger(state, stage(goal("rate_batch3", mode="sub_agent")), {"rate_batch3": ok()}, [])
    assert lg.spent_usd == pytest.approx(2.05)
    assert lg.budget_usd == 2.0
    assert lg.spent_ratio() == pytest.approx(1.025)


def test_without_a_budget_there_is_no_spent_ratio() -> None:
    lg = ledger(task_state(), stage(atomic("a")), {"a": ok()}, [])
    assert lg.budget_usd is None and lg.spent_ratio() is None


def test_checkpoints_are_the_rounds_that_ended_with_continue() -> None:
    state = task_state(rounds=rounds("continue", "repair", "continue"))
    lg = ledger(state, stage(atomic("a")), {"a": ok()}, [past(1, "continue"), past(2, "repair"), past(3, "continue")])
    assert lg.checkpoints == ("round_1", "round_3")


# --- 硬信号（附录 B.6） ---


def test_repetition_when_a_node_fails_with_the_same_error_as_last_round() -> None:
    history = [past(1, "repair", failed_nodes={"lint": error_signature("flake8: E501 at line 3")})]
    lg = ledger(task_state(rounds=rounds("repair")), LINT_STAGE, {"fix_lint": ok(), "lint": failed("flake8: E501 at line 9", role="judge")}, history)
    assert "repetition" in lg.signals


def test_no_repetition_when_the_error_changed() -> None:
    history = [past(1, "repair", failed_nodes={"lint": error_signature("flake8: E501")})]
    lg = ledger(task_state(rounds=rounds("repair")), LINT_STAGE, {"fix_lint": ok(), "lint": failed("flake8: W291", role="judge")}, history)
    assert "repetition" not in lg.signals


def test_stall_after_two_rounds_without_progress() -> None:
    history = [past(1, "continue"), past(2, "repair", failed_nodes={"a": "x"}), past(3, "replan", failed_nodes={"a": "x"})]
    lg = ledger(task_state(rounds=rounds("continue", "repair", "replan")), stage(atomic("b")), {"b": failed("y")}, history)
    assert "stall" in lg.signals
    calm = ledger(task_state(rounds=rounds("continue")), stage(atomic("b")), {"b": failed("y")}, [past(1, "continue")])
    assert "stall" not in calm.signals


def test_plan_drift_after_two_replans() -> None:
    history = [past(1, "replan", failed_nodes={"fetch_old": "下线"}), past(2, "replan", failed_nodes={"fetch_guess": "不存在"})]
    lg = ledger(task_state(rounds=rounds("replan", "replan")), stage(atomic("fetch_spec", role="fetch")), {"fetch_spec": failed("不存在", role="fetch")}, history)
    assert "plan_drift" in lg.signals


def test_parallel_anomaly_when_a_parallel_sub_agent_returns_nothing() -> None:
    s = stage(goal("market", mode="sub_agent"), goal("competitors", mode="sub_agent"), goal("pricing", mode="sub_agent"))
    lg = ledger(task_state(), s, {"market": ok(result={"n": 3}), "competitors": ok(result={}), "pricing": ok(result={"n": 2})}, [])
    assert "parallel_anomaly" in lg.signals


def test_signals_are_sorted_and_come_from_the_schema_enum() -> None:
    history = [past(1, "replan", failed_nodes={"a": "x"}), past(2, "replan", failed_nodes={"a": "x"})]
    lg = ledger(task_state(rounds=rounds("replan", "replan")), stage(atomic("a")), {"a": failed("x")}, history)
    assert list(lg.signals) == sorted(set(lg.signals))
    assert set(lg.signals) <= set(HARD_SIGNALS)
