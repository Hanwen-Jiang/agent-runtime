"""N16-R 第一段（docs/plan/sub-agent.md 第 4～6 节；总表第 8 节 N16-R）：Planner 派子 Agent 的规矩，开关 AR_SUB_AGENT_POLICY=rules（默认 off）。

1. 打开时，每个 sub_agent 的 inputs 里要有 brief：objective、scope、output 必填（sources、budget 可选）；缺了回 Planner 重写。
2. 两个以上 sub_agent 时要有一个综合节点：它的 after 列出这些 sub_agent 的全部；没有就重写。
3. fast、light 不派 sub_agent。
4. 份额：每个 sub_agent 的 budget.usd = min(brief.budget.usd, Node 自己的 budget.usd, Task 剩余 ÷ sub_agent 个数)；都没有就不写。
5. 规则写在 Planner 的 system 区；关着时提示、校验、份额都与原来一样。
"""

from __future__ import annotations

from typing import Any

import pytest

from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.planner import prompts, validate
from agentruntime_worker.planner.planner import Planner, PlannerConfig

from .planner_support import CATALOG, FakeGateway, context, plan_answer, request
from .test_n04_task_plan import RISKY_FULL
from .test_stage_runner import deliver, goal, stage

RULES = PlannerConfig(sub_agent_rules=True)
BRIEF = {"objective": "查 A 厂商的退款政策", "scope": "只查 A 厂商的公开文档，不查别家，不改文件", "output": "来源卡片的数组"}
TASK_PLAN = {"triage": {"level": "full"}, "horizon": "per_stage"}


def agent(node_id: str, brief: dict[str, Any] | None = BRIEF, **extra: Any) -> dict[str, Any]:
    inputs = {"brief": brief} if brief is not None else {}
    return goal(node_id, "sub_agent", effect_ceiling="read", tools=["web_search"], inputs=inputs, **extra)


def problems(*nodes: dict[str, Any], level: str = "full", rules: bool = True) -> list[str]:
    return validate.problems(stage(1, *nodes), TASK_PLAN, level=level, context=context(), keep_nodes=[], sub_agent_rules=rules)


# --- 校验 ---


def test_a_sub_agent_needs_a_brief_with_its_required_fields() -> None:
    assert not problems(agent("a"), deliver(after=["a"]))
    assert any("brief" in p and "a" in p for p in problems(agent("a", brief=None), deliver(after=["a"])))
    for field in ("objective", "scope", "output"):
        partial = {k: v for k, v in BRIEF.items() if k != field}
        assert any(field in p for p in problems(agent("a", brief=partial), deliver(after=["a"]))), field
    assert any("objective" in p for p in problems(agent("a", brief={**BRIEF, "objective": "  "}), deliver(after=["a"]))), "空白不算写了"


def test_two_or_more_sub_agents_need_a_synthesis_node_after_all_of_them() -> None:
    found = problems(agent("a"), agent("b"), deliver(after=["a"]))
    assert any("综合" in p for p in found)
    assert not problems(agent("a"), agent("b"), deliver(after=["a", "b"]))
    one = problems(agent("a"), deliver())
    assert not any("综合" in p for p in one), "只有一个 sub_agent 时不要求综合节点"


def test_fast_and_light_do_not_dispatch_sub_agents() -> None:
    assert any("sub_agent" in p for p in problems(agent("a"), deliver(after=["a"]), level="light"))


def test_without_the_rules_nothing_changes() -> None:
    nodes = (agent("a", brief=None), agent("b", brief=None), deliver(after=["a"]))
    assert problems(*nodes, rules=False) == validate.problems(stage(1, *nodes), TASK_PLAN, level="full", context=context(), keep_nodes=[])
    assert not any("brief" in p or "综合" in p for p in problems(*nodes, rules=False))


# --- 份额 ---


def test_budget_shares_split_the_remaining_budget() -> None:
    plan = stage(1, agent("a"), agent("b", brief={**BRIEF, "budget": {"usd": 0.1}}), agent("c", budget={"usd": 0.2}), deliver(after=["a", "b", "c"]))
    shared, notes = validate.share_sub_agent_budgets(plan, 0.9)
    budgets = {n["id"]: (n.get("budget") or {}).get("usd") for n in shared["nodes"]}
    assert budgets == {"a": 0.3, "b": 0.1, "c": 0.2, "answer": None}, "min(brief、Node 自己的、剩余 ÷ 3)"
    assert notes and plan["nodes"][0].get("budget") is None, "不改原来的 Stage"
    assert validate.share_sub_agent_budgets(stage(1, agent("a"), deliver(after=["a"])), None)[0]["nodes"][0].get("budget") is None, "没有预算就不写"
    kept, _ = validate.share_sub_agent_budgets(stage(1, agent("a", brief={**BRIEF, "budget": {"usd": 0.05}}), deliver(after=["a"])), None)
    assert kept["nodes"][0]["budget"] == {"usd": 0.05}, "没有 Task 预算时取 brief 给的"


# --- Planner ---


async def test_the_planner_sends_back_a_sub_agent_without_a_brief() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer(agent("a", brief=None), deliver(after=["a"])), plan_answer(agent("a"), deliver(after=["a"]))])
    result = await Planner(gateway, RULES).plan(request(round_seq=1))
    assert not result.fallback and len(gateway.calls("plan")) == 2
    assert "brief" in gateway.calls("plan")[1].generation.messages[-1].text
    system = gateway.calls("plan")[0].generation.messages[0].text
    assert prompts._SUB_AGENT_RULES in system and "objective" in system and "综合节点" in system, "规则写在 system 区"


async def test_the_planner_writes_the_shares_into_the_stage() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer(agent("a"), agent("b"), deliver(after=["a", "b"]))], cost_micros={"triage": 0, "plan": 0})
    state = {"goal": "比较两家", "constraints": {"budget_usd": 1.0}, "rounds": []}
    result = await Planner(gateway, RULES).plan(request(round_seq=1, task_state=state))
    budgets = {n["id"]: (n.get("budget") or {}).get("usd") for n in result.stage["nodes"]}
    assert budgets["a"] == pytest.approx(0.5) and budgets["b"] == pytest.approx(0.5)


async def test_the_planner_is_unchanged_with_the_rules_off() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer(agent("a", brief=None), agent("b", brief=None), deliver(after=["a"]))])
    result = await Planner(gateway).plan(request(round_seq=1, task_state={"goal": "x", "constraints": {"budget_usd": 1.0}, "rounds": []}))
    assert not result.fallback and len(gateway.calls("plan")) == 1
    assert all("budget" not in n for n in result.stage["nodes"] if n["id"] in ("a", "b"))
    assert prompts._SUB_AGENT_RULES not in gateway.calls("plan")[0].generation.messages[0].text, "关着时没有第 12 条"
    assert "objective" not in prompts.system_zone(CATALOG)[0].text


def test_the_switch_comes_from_the_environment() -> None:
    assert WorkerConfig.from_env({}).sub_agent_policy == "off"
    assert WorkerConfig.from_env({"AR_SUB_AGENT_POLICY": "rules"}).sub_agent_policy == "rules"
    with pytest.raises(ValueError):
        WorkerConfig.from_env({"AR_SUB_AGENT_POLICY": "always"})
