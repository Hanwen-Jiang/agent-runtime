"""S-10 验收：Reviewer（设计书 3.2.5、附录 B.4–B.9；计划 S-10 交付“Progress Check 提示”“Verdict 决策逻辑”）。

用假 Model Gateway 证明代码的部分：上下文隔离由代码限制、一次 Progress Check、不合格重问一次再回退到规则、
规则把模型的提议往上抬、完成看产出物、汇总写进 Task State。真模型上的 Verdict 准确率在 py-learning 的评测里跑。
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from agentruntime.common.v1 import enums_pb2
from agentruntime_worker.activities.types import ReviewRoundInput
from agentruntime_worker.reviewer.reviewer import Reviewer, ReviewerConfig
from agentruntime_worker.reviewer.summary import apply_state_update

from .reviewer_support import (
    TASK_ID,
    TENANT,
    FakeReviewGateway,
    answer,
    atomic,
    cont,
    done,
    escalate_human,
    failed,
    goal,
    ok,
    past,
    replan,
    request,
    retry,
    rounds,
    skipped,
    stage,
    task_state,
)
from .support import assert_valid

REPORT_STAGE = stage(
    atomic("synthesize", template="synthesize_report@v1"),
    atomic("check_cites", role="judge", template="check_citations@v1", after=("synthesize",)),
    atomic("report", role="deliver", after=("check_cites",)),
    goal_text="成稿并核对引用",
    exit_when="check_cites 的 mismatches 为 0",
)
REPORT_OK = {
    "synthesize": ok(),
    "check_cites": ok("judge", {"mismatches": 0}),
    "report": ok("deliver", {"text": "华东退款率上升主要来自物流延误（报告正文）"}),
}


def report_state() -> dict:
    return task_state(rounds=rounds("continue"), done_when="简报成稿且引用核对通过")


# --- 一次 Progress Check，独立上下文 ---


async def test_one_progress_check_call_per_review_in_a_fresh_context() -> None:
    gateway = FakeReviewGateway([done("report")])
    result = await Reviewer(gateway).review(request(REPORT_STAGE, REPORT_OK, state=report_state()))
    assert result.verdict["verdict"] == "done"
    assert len(gateway.requests) == 1
    req = gateway.requests[0]
    assert req.call.tier == ReviewerConfig().tier == "strong@medium"
    assert req.call.role == enums_pb2.ROLE_JUDGE and req.call.node_id == "review"
    assert req.call.task_id == TASK_ID and req.tenant.tenant_id == TENANT.tenant_id and req.tenant.session_id == TENANT.session_id
    assert req.generation.messages[0].cache_breakpoint, "system 区是固定的规则与 schema，打缓存断点"
    assert req.generation.HasField("output_schema")
    assert [c.purpose for c in result.calls] == ["review"] and result.calls[0].cost_usd > 0


async def test_the_rules_ask_for_the_users_language_and_keep_review_internals_out_of_facts() -> None:
    """2026-10-28 线上日志：英文界面里 Reviewer 的理由与问题是中文，“已验证的事实”里出现“代码计算出的硬信号为空，floor 为 none”。"""
    gateway = FakeReviewGateway([done("report")])
    await Reviewer(gateway).review(request(REPORT_STAGE, REPORT_OK, state=report_state()))
    system = gateway.requests[0].generation.messages[0].text
    assert "用用户的语言写" in system and "goal 是英文就写英文" in system
    assert "不提 floor、硬信号、规则" in system


async def test_the_prompt_holds_results_state_and_criteria_but_nothing_from_planning() -> None:
    """上下文隔离在代码里限制（计划 S-10 难点）：Task Plan、Planner 与 Node 的 rationale、Triage 的理由都到不了模型。"""
    s = json.loads(json.dumps(REPORT_STAGE))
    for node in s["nodes"]:
        node["rationale"] = "PLANNER-RATIONALE-为什么这样拆"
    state = report_state()
    state["task_plan"] = {"horizon": "per_stage", "notes": "SECRET-PLAN-规划过程"}
    state["triage_reasons"] = ["TRIAGE-REASON"]
    results = json.loads(json.dumps(REPORT_OK))
    results["report"]["rationale"] = "NODE-RATIONALE"
    gateway = FakeReviewGateway([done("report")])
    await Reviewer(gateway).review(request(s, results, state=state))
    prompt = gateway.prompt()
    for leaked in ("PLANNER-RATIONALE", "SECRET-PLAN", "TRIAGE-REASON", "NODE-RATIONALE", "task_plan", "rationale"):
        assert leaked not in prompt, f"{leaked} 不该进 Reviewer 的提示"
    assert "简报成稿且引用核对通过" in prompt and "check_cites 的 mismatches 为 0" in prompt, "Task 与 Stage 的验收条件都要给"
    assert "华东退款率上升主要来自物流延误（报告正文）" in prompt, "是否完成看产出物本身"


def test_the_review_activity_input_has_no_planning_fields() -> None:
    names = {f.name for f in dataclasses.fields(ReviewRoundInput)}
    # rules_only 是工作流给的开关（决策重试用完时只按规则判定，TMP 计划第 4 节）；verification 是代码记下的执行事实（第 11.15 节）。都不带规划过程。
    assert names == {"tenant", "task_id", "round_seq", "task_state", "stage_results", "done_when", "stage", "history", "rules_only", "verification"}


# --- 规则先于模型 ---


async def test_the_model_cannot_go_below_the_code_floor() -> None:
    lint_stage = stage(goal("fix_lint"), atomic("lint", role="judge", template="run_tests@v1", executor="program", effect="read", after=("fix_lint",)))
    results = {"fix_lint": ok(), "lint": failed("flake8: E501", role="judge")}
    history = [past(1, "repair", failed_nodes={"lint": "flake8: E501"})]
    gateway = FakeReviewGateway([retry("fix_lint")])
    result = await Reviewer(gateway).review(request(lint_stage, results, history=history))
    assert result.verdict["verdict"] == "repair" and result.verdict["repair"]["strategy"] == "change_method"
    assert result.raised and result.floor.step == "repair_change_method"
    assert "repair_change_method" in gateway.prompt(), "代码已判定的 floor 告诉模型，免得它白费力气"


async def test_the_model_may_escalate_above_the_floor() -> None:
    lint_stage = stage(goal("fix_lint"), atomic("lint", role="judge", after=("fix_lint",)))
    gateway = FakeReviewGateway([escalate_human("lint 规则能放宽吗？")])
    result = await Reviewer(gateway).review(request(lint_stage, {"fix_lint": ok(), "lint": failed("E501", role="judge")}))
    assert result.verdict["verdict"] == "escalate" and not result.raised


async def test_code_signals_stay_even_if_the_model_leaves_them_out() -> None:
    s = stage(atomic("fetch_spec", role="fetch"))
    history = [past(1, "replan", failed_nodes={"fetch_old": "下线"}), past(2, "replan", failed_nodes={"fetch_guess": "不存在"})]
    gateway = FakeReviewGateway([answer({"verdict": "replan", "signals": ["context_health"], "replan": {"change_shape": True}})])
    result = await Reviewer(gateway).review(request(s, {"fetch_spec": failed("规格页不存在", role="fetch")}, history=history))
    assert {"plan_drift", "context_health"} <= set(result.verdict["signals"])


# --- 不合格时重问一次，再回退到规则 ---


async def test_an_invalid_answer_is_asked_again_once_with_the_problems() -> None:
    gateway = FakeReviewGateway(["我觉得已经完成了", done("report")])
    result = await Reviewer(gateway).review(request(REPORT_STAGE, REPORT_OK, state=report_state()))
    assert len(gateway.requests) == 2
    retry_prompt = gateway.prompt(1)
    assert "我觉得已经完成了" in retry_prompt, "重问时带上上一次的回答"
    assert result.verdict["verdict"] == "done" and not result.fallback


async def test_a_verdict_that_breaks_the_schema_counts_as_invalid() -> None:
    broken = answer({"verdict": "repair"})  # repair 必须带 repair.nodes 与 strategy
    gateway = FakeReviewGateway([broken, done("report")])
    result = await Reviewer(gateway).review(request(REPORT_STAGE, REPORT_OK, state=report_state()))
    assert len(gateway.requests) == 2 and "repair" in gateway.prompt(1)
    assert result.verdict["verdict"] == "done"


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        (REPORT_OK, "done"),
        ({"synthesize": ok(), "check_cites": ok("judge", {"mismatches": 0})}, "continue"),
        ({"synthesize": ok(), "check_cites": failed("3 处不符", role="judge"), "report": skipped("check_cites", role="deliver")}, "repair"),
    ],
)
async def test_two_invalid_answers_fall_back_to_the_rules(results: dict, expected: str) -> None:
    gateway = FakeReviewGateway(["乱答一通", "还是乱答"])
    s = REPORT_STAGE if "report" in results else stage(*REPORT_STAGE["nodes"][:2])
    result = await Reviewer(gateway).review(request(s, results, state=report_state()))
    assert result.fallback and result.problems
    assert result.verdict["verdict"] == expected
    assert_valid("verdict", result.verdict)


async def test_a_gateway_error_falls_back_to_the_rules() -> None:
    gateway = FakeReviewGateway([RuntimeError("模型不可用"), RuntimeError("模型不可用")])
    result = await Reviewer(gateway).review(request(REPORT_STAGE, REPORT_OK, state=report_state()))
    assert result.fallback and result.verdict["verdict"] == "done"


async def test_fast_rounds_follow_the_rules_without_calling_a_model() -> None:
    """设计书 3.2.1：Triage 为 fast 时 Reviewer 退化为规则，只做格式检查与异步写回。"""
    s = stage(atomic("answer", role="deliver"), goal_text="回复用户这条消息", exit_when="answer 给出回复")
    state = task_state(rounds=rounds(triage="fast"), done_when="回答用户的问题")
    gateway = FakeReviewGateway([])
    result = await Reviewer(gateway).review(request(s, {"answer": ok("deliver", {"text": "好的"})}, state=state))
    assert gateway.requests == [] and result.calls == []
    assert result.verdict["verdict"] == "done" and result.verdict["done"] == {"deliver_node": "answer"}


# --- 完成看产出物 ---


async def test_done_without_a_deliverable_is_not_done() -> None:
    s = stage(goal("market", mode="sub_agent"), atomic("draft", after=("market",)), exit_when="草稿完成")
    gateway = FakeReviewGateway([done("draft")])
    result = await Reviewer(gateway).review(request(s, {"market": ok(), "draft": ok(result={"pages": 12})}))
    assert result.verdict["verdict"] == "continue" and result.raised


async def test_completion_from_the_progress_check_drives_the_budget_rule() -> None:
    state = task_state(rounds=rounds("continue", costs=[0.5], current_cost=0.35), budget=1.0)
    s = stage(goal("rate_batch2", mode="sub_agent"))
    results = {"rate_batch2": ok(result={"rated_total": 20, "total": 50})}
    low = await Reviewer(FakeReviewGateway([cont(completion=0.3)])).review(request(s, results, state=state))
    assert low.completion == 0.3
    assert low.verdict["verdict"] == "escalate" and "budget_burn" in low.verdict["signals"]
    high = await Reviewer(FakeReviewGateway([cont(completion=0.7)])).review(request(s, results, state=state))
    assert high.verdict["verdict"] == "continue"


# --- 汇总：写进 Task State 的内容 ---


async def test_the_state_update_is_checked_and_capped() -> None:
    update = {
        "facts": ["华东退款率明显高于其他区域", 42, "", *[f"事实 {i}" for i in range(30)]],
        "assumptions_broken": ["工单原因字段可靠"],
        "assumptions_verified": [],
        "open_questions": ["物流商是否更换过"],
        "secret_plan": ["不认识的键丢掉"],
    }
    gateway = FakeReviewGateway([answer({"verdict": "continue"}, state_update=update)])
    result = await Reviewer(gateway, ReviewerConfig(max_state_items=10)).review(request(REPORT_STAGE, REPORT_OK, state=report_state()))
    su = result.state_update
    assert set(su) <= {"facts", "assumptions_verified", "assumptions_broken", "open_questions", "artifacts"}
    assert su["facts"][0] == "华东退款率明显高于其他区域" and len(su["facts"]) == 10
    assert all(isinstance(f, str) and f for f in su["facts"])
    assert su["assumptions_broken"] == ["工单原因字段可靠"] and su["open_questions"] == ["物流商是否更换过"]


async def test_artifacts_come_from_the_results_and_stay_inside_the_tenant() -> None:
    results = {
        "synthesize": ok(result={"file": "obj://t1/tsk_x/report.md", "refs": ["obj://t1/tsk_x/data.csv", "obj://t2/other/leak.csv"]}),
        "check_cites": ok("judge", {"mismatches": 0}),
        "report": ok("deliver", {"text": "正文"}),
    }
    gateway = FakeReviewGateway([done("report")])
    result = await Reviewer(gateway).review(request(REPORT_STAGE, results, state=report_state()))
    assert result.state_update["artifacts"] == ["obj://t1/tsk_x/data.csv", "obj://t1/tsk_x/report.md"], "只收本租户前缀的对象，按字典序"


async def test_a_replans_stuck_reason_is_written_into_the_task_state() -> None:
    s = stage(goal("read_tickets", mode="sub_agent"), atomic("analyze", after=("read_tickets",)))
    results = {"read_tickets": ok(result={"empty_reason_share": 0.62}), "analyze": failed("62% 的原因字段为空")}
    gateway = FakeReviewGateway([replan(keep=["read_tickets"], stuck="改从退款备注文本归纳原因")])
    result = await Reviewer(gateway).review(request(s, results, state=task_state(assumptions=["工单原因字段可靠"])))
    assert result.verdict["replan"]["keep_nodes"] == ["read_tickets"]
    assert any("改从退款备注文本归纳原因" in f for f in result.state_update["facts"]), "卡住的原因写进 Task State，让下一次尝试真正不同"


def test_applying_a_state_update_keeps_the_task_state_valid() -> None:
    """工作流按汇总改 Task State：事实去重追加；验证过的假设转成事实，被证伪的从假设里拿掉并记成事实；产物去重。"""
    state = task_state(facts=["已知"], assumptions=["工单原因字段可靠", "物流数据完整"])
    apply_state_update(
        state,
        {
            "facts": ["已知", "新事实"],
            "assumptions_verified": ["物流数据完整"],
            "assumptions_broken": ["工单原因字段可靠"],
            "open_questions": ["物流商是否更换过"],
            "artifacts": ["obj://t1/x/a.csv", "obj://t1/x/a.csv"],
        },
    )
    assert state["facts"] == ["已知", "新事实", "物流数据完整", "假设不成立：工单原因字段可靠"]
    assert state["assumptions"] == []
    assert state["open_questions"] == ["物流商是否更换过"]
    assert state["artifacts"] == ["obj://t1/x/a.csv"]
    assert_valid("task_state", state)


async def test_reviewer_reasons_are_recorded_for_the_trace() -> None:
    lint_stage = stage(goal("fix_lint"), atomic("lint", role="judge", after=("fix_lint",)))
    history = [past(1, "repair", failed_nodes={"lint": "E501"}), past(2, "repair", step="repair_change_method", failed_nodes={"lint": "E501"})]
    result = await Reviewer(FakeReviewGateway([retry("fix_lint")])).review(request(lint_stage, {"fix_lint": ok(), "lint": failed("E501", role="judge")}, history=history))
    assert result.verdict["verdict"] == "replan"
    assert result.reasons and any("3" in r for r in result.reasons)
