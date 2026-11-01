"""S-09：完整规划与 replan（计划 S-09 难点“结构化输出”“不能编造模板”“缓存友好”“规划成本”；验收“replan 保留已验证的 Node”）。"""

from __future__ import annotations

from typing import Any

import pytest

from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.executor import keys
from agentruntime_worker.planner.planner import Planner
from agentruntime_worker.planner.triage import UNCLASSIFIED_TASK_FAMILY
from agentruntime_worker.policy.hooks import HookContext, budget_check

from .planner_support import FakeGateway, context, last_round, plan_answer, prompt_text, request, role, signals
from .support import assert_valid

RISKY_FULL = signals("new_task", family="analytics.attribution", confidence=0.6, effect="read")


def atomic(node_id: str, template: str, executor: str, effect: str, role_: str, **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"id": node_id, "kind": "atomic", "template": template, "executor": executor, "effect": effect, "role": role_}
    if executor == "model":
        node["output"] = "generate"
    node.update(extra)
    return node


def problems_text(retry) -> str:
    """重问时追加的最后一条消息：不合格之处。"""
    return retry.generation.messages[-1].text


async def plan(gateway: FakeGateway, **kw: Any):
    return await Planner(gateway).plan(request(**kw))


# --- 结构化输出：合格就用，代码定下 Triage、seq 与幂等键 ---


async def test_a_valid_plan_is_used_on_the_strong_tier() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer()])
    result = await plan(gateway, round_seq=1)
    assert result.triage_level == "full" and not result.fallback
    [call] = gateway.calls("plan")
    assert call.call.tier == "strong@medium" and call.call.role == role("plan")
    assert call.tenant.tenant_id == "t1"
    assert call.generation.output_schema.ByteSize() > 0
    assert result.stage["seq"] == 1, "seq 由代码定为本轮序号"
    assert [n["id"] for n in result.stage["nodes"]] == ["q_region"]
    assert_valid("stage", result.stage)
    assert_valid("task_plan", result.task_plan)


async def test_the_triage_level_in_the_task_plan_is_decided_in_code() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer(level="light")])
    result = await plan(gateway)
    assert result.triage_level == "full"
    assert result.task_plan["triage"]["level"] == "full", "模型改不了 Triage 级别"


async def test_idempotency_keys_are_generated_by_code() -> None:
    write = atomic("commit", "commit_progress@v1", "program", "write_reversible", "act", idempotency_key="model-made-up")
    gateway = FakeGateway(triage=signals(effect="write_reversible"), plan=[plan_answer(write)])
    result = await plan(gateway, ctx=context(risk="write_reversible", ceiling="write_reversible"))
    [node] = result.stage["nodes"]
    assert node["idempotency_key"] == keys.idempotency_key("t1", request().task_id, "commit", node.get("inputs", {}))


# --- 不合格：带上错误重问一次，仍不合格就回退到保守规划 ---


async def test_an_invalid_plan_is_asked_again_with_the_errors() -> None:
    broken = plan_answer()
    del broken["stage"]["exit_when"]
    gateway = FakeGateway(triage=RISKY_FULL, plan=[broken, plan_answer()])
    result = await plan(gateway)
    first, second = gateway.calls("plan")
    n = len(first.generation.messages)
    assert [m.SerializeToString() for m in second.generation.messages[:n]] == [m.SerializeToString() for m in first.generation.messages], "重问是在原对话后追加"
    assert "exit_when" in problems_text(second)
    assert not result.fallback and result.stage["exit_when"] == "数据齐全"


async def test_two_invalid_plans_fall_back_to_one_tool_loop_goal_node() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=["这不是 JSON", {"stage": "也不对"}])
    result = await plan(gateway, round_seq=2)
    assert result.fallback and result.problems
    assert result.triage_level == "full"
    [node] = result.stage["nodes"]
    assert node["kind"] == "goal" and node["mode"] == "tool_loop"
    assert result.stage["seq"] == 2
    assert_valid("stage", result.stage)
    assert_valid("task_plan", result.task_plan)
    assert len(gateway.calls("plan")) == 2, "最多问两次"


async def test_a_model_error_on_the_retry_also_falls_back() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=["不是 JSON", RuntimeError("模型超时")])
    result = await plan(gateway)
    assert result.fallback


@pytest.mark.parametrize(
    ("node", "needle"),
    [
        (atomic("q", "made_up@v1", "program", "read", "fetch"), "made_up@v1"),
        ({"id": "batch", "kind": "goal", "goal": "逐条处理", "done_when": "都处理完", "mode": "template", "recipe": "nope@v1", "output_schema": {"type": "object"}}, "nope@v1"),
        ({"id": "look", "kind": "goal", "goal": "查", "done_when": "查到", "mode": "tool_loop", "tools": ["teleport"], "output_schema": {"type": "object"}}, "teleport"),
        (atomic("draft", "summarize@v1", "model", "none", "transform", quality={"type": "revise", "judge": "ghost_judge@v1", "max_rounds": 2}), "ghost_judge@v1"),
    ],
)
async def test_templates_recipes_and_tools_outside_the_catalog_are_rejected(node: dict[str, Any], needle: str) -> None:
    """不能编造模板：校验在代码里做。"""
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer(node), plan_answer()])
    result = await plan(gateway)
    _, retry = gateway.calls("plan")
    assert needle in problems_text(retry)
    assert [n["id"] for n in result.stage["nodes"]] == ["q_region"]


@pytest.mark.parametrize(
    ("node", "ctx_kw", "needle"),
    [
        (atomic("commit", "commit_progress@v1", "program", "write_reversible", "act"), {"risk": "read", "ceiling": "read"}, "write_reversible"),
        (atomic("pay", "book_and_pay@v1", "program", "write_reversible", "act"), {"risk": "write_irreversible", "ceiling": "write_irreversible"}, "write_irreversible"),
        ({"id": "fix", "kind": "goal", "goal": "修", "done_when": "过", "mode": "tool_loop", "effect_ceiling": "read", "tools": ["repo_edit"], "output_schema": {"type": "object"}}, {"risk": "write_reversible", "ceiling": "write_reversible"}, "repo_edit"),
    ],
    ids=["above_ceiling", "below_template", "tool_above_node_ceiling"],
)
async def test_effect_rules_are_checked_in_code(node: dict[str, Any], ctx_kw: dict[str, str], needle: str) -> None:
    gateway = FakeGateway(triage=signals(effect=ctx_kw["ceiling"]), plan=[plan_answer(node), plan_answer()])
    await plan(gateway, ctx=context(**ctx_kw))
    _, retry = gateway.calls("plan")
    assert needle in problems_text(retry)


@pytest.mark.parametrize(
    "node",
    [
        atomic("read_pricing", "answer@v1", "model", "none", "fetch"),
        atomic("fix_pricing", "answer@v1", "model", "write_reversible", "act"),
        # 节点自称 program，模板却是 model：按模板算，模型模板照样没有工具。
        atomic("run_tests", "answer@v1", "program", "read", "fetch"),
    ],
    ids=["model_fetch", "model_act", "template_is_model"],
)
async def test_a_model_node_cannot_fetch_or_act(node: dict[str, Any]) -> None:
    """2026-10-28 真模型实验第 3.1 条：真 Planner 把读文件、跑测试、改代码拆成 answer@v1 的 Atomic 节点，节点没有工具，照样 succeeded，
    内容是“工具不可用”。model 节点拿不到外部信息、也做不了动作：fetch 与 act 要用 program 模板或带 tools 的 tool_loop Goal Node。"""
    gateway = FakeGateway(triage=signals(effect="write_reversible"), plan=[plan_answer(node), plan_answer()])
    result = await plan(gateway, ctx=context(risk="write_reversible", ceiling="write_reversible"))
    _, retry = gateway.calls("plan")
    text = problems_text(retry)
    assert node["id"] in text and "tool_loop" in text, text
    assert [n["id"] for n in result.stage["nodes"]] == ["q_region"]


async def test_model_nodes_may_still_transform_and_deliver() -> None:
    nodes = [
        atomic("q_region", "sql_query@v1", "program", "read", "fetch"),
        atomic("draft", "rewrite@v1", "model", "none", "transform", after=["q_region"]),
        atomic("report", "answer@v1", "model", "none", "deliver", after=["draft"]),
    ]
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer(*nodes)])
    result = await plan(gateway)
    assert len(gateway.calls("plan")) == 1 and not result.fallback, "取数用 program、加工与交付用 model，合格"


async def test_the_rules_say_only_tool_loop_goal_nodes_have_tools() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer()])
    await plan(gateway)
    [call] = gateway.calls("plan")
    system = call.generation.messages[0].text
    assert "model 节点没有工具" in system and "tool_loop" in system


SPENT_STATE = {
    "task_id": "tsk_01J" + "0" * 22 + "B", "session_id": "ses_01J" + "0" * 22 + "S", "goal": "讲清仓库结构", "done_when": "回答用户的问题",
    "constraints": {"budget_usd": 1.0},
    "rounds": [{"seq": 1, "stage_goal": "探索", "status": "completed", "verdict": "repair", "cost_usd": 0.1758}],
}


async def test_node_and_stage_budgets_are_capped_at_what_the_task_has_left() -> None:
    """2026-10-28 线上日志：Planner 给 Node 0.9 美元，Task 只剩 0.8242 美元，budget_check 拒绝了整个 Node，白花一轮。
    Planner 不知道剩多少（session_budget_remaining_usd 没人填）：由 Task State 算出剩余，提示里写明，代码把超出的预算压到剩余。"""
    big = {"id": "explore", "kind": "goal", "goal": "读仓库", "done_when": "读完", "mode": "tool_loop", "tools": ["repo_read"],
           "output_schema": {"type": "object"}, "budget": {"usd": 0.9}}
    capped_atomic = atomic("q_region", "sql_query@v1", "program", "read", "fetch", budget={"max_cost_usd": 2.0})
    answer = plan_answer(big, capped_atomic)
    answer["stage"]["budget"] = {"usd": 5.0}
    gateway = FakeGateway(triage=RISKY_FULL, plan=[answer])
    result = await plan(gateway, task_state=SPENT_STATE, round_seq=2)
    nodes = {n["id"]: n for n in result.stage["nodes"]}
    left = round(0.8242 - sum(c.cost_usd for c in result.calls), 6)
    assert nodes["explore"]["budget"]["usd"] == pytest.approx(left)
    assert nodes["q_region"]["budget"]["max_cost_usd"] == pytest.approx(left)
    assert result.stage["budget"]["usd"] == pytest.approx(left)
    assert any(f"{left:g}" in a for a in result.adjustments)
    [call] = gateway.calls("plan")
    assert "Task 剩余预算：$0.82" in prompt_text(call)


async def test_the_cap_leaves_room_for_what_this_round_of_planning_cost() -> None:
    """budget_check 把这一轮规划的花费也算作已花：基线 50 题里 6 题的 Node 被压到“规划之前的剩余”，还是被整个拒绝，白费一轮。"""
    big = {"id": "explore", "kind": "goal", "goal": "读仓库", "done_when": "读完", "mode": "tool_loop", "tools": ["repo_read"],
           "output_schema": {"type": "object"}, "budget": {"usd": 0.9}}
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer(big)], cost_micros={"triage": 2_000, "plan": 10_000})
    result = await plan(gateway, task_state=SPENT_STATE, round_seq=2)
    node = result.stage["nodes"][0]
    spent = round(0.1758 + sum(c.cost_usd for c in result.calls), 6)
    assert spent > 0.1758
    assert node["budget"]["usd"] == pytest.approx(1.0 - spent)
    tenant = Tenant(tenant_id="t1", user_id="u1", session_id="s1")
    verdict = budget_check(HookContext(tenant, "tsk", 2, "before_node", {"node": node, "budget": {"task_usd": 1.0, "spent_usd": spent}}))
    assert verdict["decision"] == "allow", "压过之后 budget_check 要放行，否则还是白费一轮"


async def test_budgets_within_what_is_left_are_kept() -> None:
    small = {"id": "explore", "kind": "goal", "goal": "读仓库", "done_when": "读完", "mode": "tool_loop", "tools": ["repo_read"],
             "output_schema": {"type": "object"}, "budget": {"usd": 0.3}}
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer(small)])
    result = await plan(gateway, task_state=SPENT_STATE, round_seq=2)
    assert result.stage["nodes"][0]["budget"] == {"usd": 0.3} and not result.adjustments


async def test_a_task_without_a_family_is_recorded_as_unclassified() -> None:
    """2026-10-28 真模型实验第 5 节：分诊判断不了任务族、规划模型也没写时，Task Plan 里没有 task_family，收尾写回的 trace 里就是空的，
    Learning 按任务族过滤时整个 Task 都看不到（失败的 Task 全在里面）。给一个固定的“未分类”族：至少能被看到、被计数。"""
    unnamed = plan_answer()
    del unnamed["task_plan"]["triage"]["task_family"]
    gateway = FakeGateway(triage=signals("new_task", family=None, confidence=0.6), plan=[unnamed])
    result = await plan(gateway)
    assert result.task_plan["triage"]["task_family"] == UNCLASSIFIED_TASK_FAMILY
    assert_valid("task_plan", result.task_plan)


async def test_a_family_from_triage_or_the_planner_is_kept() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer()])
    result = await plan(gateway)
    assert result.task_plan["triage"]["task_family"] == "analytics.attribution"


async def test_a_bad_dag_is_rejected() -> None:
    cyclic = [atomic("a", "sql_query@v1", "program", "read", "fetch", after=["b"]), atomic("b", "sql_query@v1", "program", "read", "fetch", after=["a"])]
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer(*cyclic), plan_answer()])
    result = await plan(gateway)
    assert len(gateway.calls("plan")) == 2 and not result.fallback


# --- replan：只重做剩余部分，保留已验证的 Node ---


REPLAN = last_round({"verdict": "replan", "reason": "locale 的假设被证伪", "replan": {"keep_nodes": ["read_log"], "stuck_reason": "CI 镜像的 locale 是 C"}})
STATE = {"task_id": "tsk_01J" + "0" * 22 + "P", "session_id": "ses_01J" + "0" * 22 + "S", "goal": "修好 test_currency_format", "done_when": "测试通过",
         "rounds": [{"seq": 1, "stage_goal": "读 CI 日志并修复", "status": "completed", "verdict": "replan"}]}
PRIOR_PLAN = {"triage": {"level": "full", "task_family": "engineering.ci_fix"}, "horizon": "full_dag"}


async def test_replan_sees_the_kept_nodes_and_why_the_last_round_failed() -> None:
    fix = {"id": "fix_locale", "kind": "goal", "goal": "按 C locale 修复", "done_when": "测试通过", "mode": "tool_loop", "tools": ["repo_read", "repo_edit"], "effect_ceiling": "write_reversible", "output_schema": {"type": "object"}}
    gateway = FakeGateway(plan=[plan_answer(fix, level="replan")])
    result = await plan(gateway, round_seq=2, task_state=STATE, task_plan=PRIOR_PLAN, last_round=REPLAN, ctx=context(risk="write_reversible", ceiling="write_reversible"))
    assert result.triage_level == "replan"
    assert not gateway.calls("triage"), "Reviewer 判了 replan，不用再分诊"
    [call] = gateway.calls("plan")
    text = prompt_text(call)
    assert "read_log" in text and "CI 镜像的 locale 是 C" in text and "engineering.ci_fix" in text
    assert result.task_plan["triage"]["level"] == "replan"
    assert result.stage["seq"] == 2


async def test_replan_must_not_redo_kept_nodes() -> None:
    redo = atomic("read_log", "run_tests@v1", "program", "read", "fetch")
    gateway = FakeGateway(plan=[plan_answer(redo, level="replan"), plan_answer(redo, level="replan")])
    result = await plan(gateway, round_seq=2, task_state=STATE, task_plan=PRIOR_PLAN, last_round=REPLAN, ctx=context(risk="write_reversible", ceiling="write_reversible"))
    _, retry = gateway.calls("plan")
    assert "read_log" in problems_text(retry)
    assert result.fallback
    assert "read_log" not in [n["id"] for n in result.stage["nodes"]], "保守规划也不重做已验证的 Node"


# --- 缓存友好与规划成本 ---


async def test_the_system_zone_is_identical_across_tasks_and_ends_with_a_cache_breakpoint() -> None:
    a = FakeGateway(triage=RISKY_FULL, plan=[plan_answer()])
    b = FakeGateway(triage=RISKY_FULL, plan=[plan_answer()])
    await plan(a, ctx=context(risk="read"))
    await plan(b, ctx=context(risk="write_reversible", ceiling="write_reversible"), task_state=STATE)
    msgs_a, msgs_b = a.calls("plan")[0].generation.messages, b.calls("plan")[0].generation.messages
    cut = next(i for i, m in enumerate(msgs_a) if m.cache_breakpoint)
    assert [m.SerializeToString() for m in msgs_a[: cut + 1]] == [m.SerializeToString() for m in msgs_b[: cut + 1]]
    system = "".join(m.text for m in msgs_a[: cut + 1])
    assert all(t in system for t in ("sql_query@v1", "ci_fix@v2", "repo_edit")), "Node Catalog 在 system 区"
    assert "分析上季度退款率上升的原因" not in system, "本轮的消息在缓存断点之后"


async def test_model_calls_are_reported_with_tier_vendor_and_cost() -> None:
    broken = plan_answer()
    del broken["stage"]["goal"]
    gateway = FakeGateway(triage=RISKY_FULL, plan=[broken, plan_answer()], vendor="vendor_x", cost_micros={"triage": 300, "plan": 15_000})
    result = await plan(gateway)
    assert [(c.purpose, c.tier, c.vendor) for c in result.calls] == [("triage", "small@low", "vendor_x"), ("plan", "strong@medium", "vendor_x"), ("plan", "strong@medium", "vendor_x")]
    assert sum(c.cost_usd for c in result.calls) == pytest.approx(0.0003 + 0.015 * 2)
