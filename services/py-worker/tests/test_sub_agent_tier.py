"""N16-R：Planner 给单个 sub_agent 写的 tier（goal_node 的 tier，契约 af5fced，docs/plan/sub-agent.md 第 4 节）。

- 只能往小调：不大于子 Agent 的默认档 mid@low（档按 small < mid < strong、推理强度按 low < medium < high 比较）。
- 开关 AR_SUB_AGENT_POLICY=rules 打开时 Planner 才校验它、tool_loop 才按它选档；关着时与原来一样（子 Agent 用主流程的档）。
- tool_loop 在 activity 里直接读 Goal Node 的 tier，不改工作流，不改命令序列。万一来了不合规的 tier（校验之外的路径），
  照旧用 mid@low，不往大调。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentruntime.common.v1 import enums_pb2
from agentruntime_worker.context.builder import ContextBuilder
from agentruntime_worker.executor.tool_loop import SUB_AGENT_TIER, TOOL_LOOP_TIER, ModelToolLoop, sub_agent_tier
from agentruntime_worker.planner import prompts

from .context_support import ContextGateway, config, objects
from .planner_support import CATALOG
from .test_context_usage import answered
from .test_planner_sub_agent import agent, problems
from .test_stage_runner import deliver
from .test_tool_loop_model import step_input


@pytest.mark.parametrize("tier", ["small@low", "small@high", "mid@low"])
def test_a_smaller_tier_is_accepted(tier: str) -> None:
    assert not [p for p in problems(agent("a", tier=tier), deliver(after=["a"])) if "tier" in p]


@pytest.mark.parametrize("tier", ["mid@medium", "strong@medium", "strong@high", "huge@low", "mid@extreme"])
def test_a_tier_above_mid_low_or_unknown_is_sent_back(tier: str) -> None:
    found = [p for p in problems(agent("a", tier=tier), deliver(after=["a"])) if "tier" in p]
    assert found and "a" in found[0] and SUB_AGENT_TIER in found[0], found


def test_without_the_rules_the_tier_is_not_checked() -> None:
    assert not [p for p in problems(agent("a", tier="strong@high"), deliver(after=["a"]), rules=False) if "tier" in p]


def test_the_prompt_says_the_tier_can_only_go_down() -> None:
    def text(rules: bool) -> str:
        return "".join(m.text for m in prompts.system_zone(CATALOG, sub_agent_rules=rules))

    assert "tier" in prompts._SUB_AGENT_RULES and SUB_AGENT_TIER in text(True)
    assert SUB_AGENT_TIER not in text(False), "开关关着时提示不变"


@pytest.mark.parametrize(
    ("node_tier", "expected"),
    [(None, SUB_AGENT_TIER), ("small@low", "small@low"), ("mid@low", "mid@low"), ("strong@high", SUB_AGENT_TIER), ("bad", SUB_AGENT_TIER)],
)
def test_the_sub_agent_tier_follows_the_node_only_downwards(node_tier: str | None, expected: str) -> None:
    assert sub_agent_tier(SUB_AGENT_TIER, node_tier) == expected


async def test_tool_loop_steps_use_the_node_tier(tmp_path: Path) -> None:
    gateway = ContextGateway(steps=[answered(200_000)] * 4)
    loop = ModelToolLoop(gateway, ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=100_000)), sub_agent_tier=SUB_AGENT_TIER)
    isolated = step_input([])
    isolated.isolated = True
    isolated.node = {**isolated.node, "tier": "small@low"}
    await loop.step(isolated)
    main = step_input([])
    main.node = {**main.node, "tier": "small@low"}
    await loop.step(main)
    tiers = [r.call.tier for r in gateway.requests if r.call.role == enums_pb2.ROLE_ACT]
    assert tiers == ["small@low", TOOL_LOOP_TIER], "只有子 Agent 按 Node 的 tier；主流程不换"

    plain = ModelToolLoop(gateway, ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=100_000)))
    gateway.requests.clear()
    await plain.step(isolated)
    assert [r.call.tier for r in gateway.requests if r.call.role == enums_pb2.ROLE_ACT] == [TOOL_LOOP_TIER], "开关关着时不换"
