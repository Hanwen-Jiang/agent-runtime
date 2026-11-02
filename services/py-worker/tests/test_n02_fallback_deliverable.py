"""docs/plan/feedback-2026-11-01.md N-02：没有模型判定时不能放行坏的交付；模型评审拿不到的原因要看得见。

线上日志（2026-11-01）：第 8 轮的交付写着 “$diagnose_test_failure.result.root_cause（unresolved）”，Reviewer 回退到规则后判 done；
第 4 轮的交付是空的。界面上只显示 “No model review was available, so the rules decided”，看不出为什么回退。
"""

from __future__ import annotations

import pytest

from agentruntime_worker.reviewer.reviewer import Reviewer
from agentruntime_worker.reviewer.rules import Thresholds, deliverable_problem, enforce, floor
from agentruntime_worker.reviewer.signals import ledger

from .reviewer_support import CATALOG, FakeReviewGateway, atomic, done, goal, ok, request, rounds, stage, task_state
from .support import assert_valid

PLACEHOLDER_ANSWER = {
    "text": "I can't answer this because the node's input variables were never resolved: Root cause: $diagnose_test_failure.result.root_cause (unresolved)"
}
STAGE = stage(goal("verify_tests"), atomic("deliver_results", role="deliver", after=("verify_tests",)), goal_text="验证并交付", exit_when="交付了")


def results_with(deliverable: object) -> dict:
    return {"verify_tests": ok(), "deliver_results": ok("deliver", deliverable)}


def decide(results: dict, proposal: dict | None) -> dict:
    state = task_state(rounds=rounds())
    lg = ledger(state, STAGE, results, [])
    fl = floor(lg, results, completion=None, thresholds=Thresholds())
    verdict = enforce(None if proposal is None else proposal["verdict"], floor=fl, ledger=lg, stage=STAGE, results=results, catalog=CATALOG).verdict
    assert_valid("verdict", verdict)
    return verdict


@pytest.mark.parametrize("value", [None, "", "   ", {"text": ""}, {"text": " ", "files": []}])
def test_empty_deliverables_are_found(value: object) -> None:
    found = deliverable_problem(value)
    assert found is not None and found[1] == "rule_deliverable_empty"


def test_unresolved_placeholders_are_found() -> None:
    found = deliverable_problem(PLACEHOLDER_ANSWER)
    assert found is not None and found[1] == "rule_deliverable_unresolved" and found[2] == {"references": ["$diagnose_test_failure.result"]}


@pytest.mark.parametrize("value", [{"text": "测试全部通过：24 passed"}, "价格是 $5，result 字段……", {"text": "写成 $$x.result 表示字面量"}])
def test_good_deliverables_pass(value: object) -> None:
    assert deliverable_problem(value) is None


@pytest.mark.parametrize("deliverable", [PLACEHOLDER_ANSWER, {"text": ""}])
def test_the_rules_do_not_deliver_a_bad_deliverable(deliverable: object) -> None:
    verdict = decide(results_with(deliverable), None)
    assert verdict["verdict"] == "replan", verdict
    assert verdict["reason_code"] in ("rule_deliverable_unresolved", "rule_deliverable_empty")
    assert "deliver_results" not in (verdict["replan"].get("keep_nodes") or []), "要重做的交付不能被保留"
    assert "verify_tests" in verdict["replan"]["keep_nodes"]


def test_a_model_done_on_a_bad_deliverable_is_raised_too() -> None:
    verdict = decide(results_with(PLACEHOLDER_ANSWER), done("deliver_results"))
    assert verdict["verdict"] == "replan" and verdict["reason_code"] == "rule_deliverable_unresolved"


def test_a_good_deliverable_is_still_done() -> None:
    assert decide(results_with({"text": "24 个测试因缺少 psycopg 无法导入；装上 trade 依赖后 24 passed"}), None)["verdict"] == "done"


async def test_the_cause_of_a_fallback_is_recorded() -> None:
    gateway = FakeReviewGateway([RuntimeError("模型不可用"), RuntimeError("模型不可用")])
    good = results_with({"text": "完成"})
    result = await Reviewer(gateway).review(request(STAGE, good, state=task_state(rounds=rounds())))
    assert result.fallback and result.verdict["reason_code"] == "rule_fallback"
    assert result.verdict["reason_params"] == {"cause": "model_call_failed", "error": "runtime_error"}
    assert_valid("verdict", result.verdict)


async def test_timeout_fallback_has_a_distinct_reason() -> None:
    gateway = FakeReviewGateway([TimeoutError("review timed out"), TimeoutError("review timed out")])
    result = await Reviewer(gateway).review(request(STAGE, results_with({"text": "完成"}), state=task_state(rounds=rounds())))
    assert result.fallback and result.verdict["reason_code"] == "rule_fallback"
    assert result.verdict["reason_params"] == {"cause": "model_call_timeout", "error": "timeout_error"}
    assert_valid("verdict", result.verdict)


async def test_the_cause_of_invalid_answers_is_recorded_without_the_text() -> None:
    gateway = FakeReviewGateway(["乱答一通", "还是乱答"])
    result = await Reviewer(gateway).review(request(STAGE, results_with({"text": "完成"}), state=task_state(rounds=rounds())))
    params = result.verdict["reason_params"]
    assert params["cause"] == "invalid_answer" and "乱答" not in str(params)


async def test_a_fast_round_says_why_there_was_no_model_review() -> None:
    s = stage(atomic("answer", role="deliver"), goal_text="回复用户这条消息", exit_when="answer 给出回复")
    result = await Reviewer(FakeReviewGateway([])).review(request(s, {"answer": ok("deliver", {"text": "你好"})}, state=task_state(rounds=rounds(triage="fast"))))
    assert result.verdict["verdict"] == "done" and result.verdict["reason_params"] == {"cause": "fast_reply"}
