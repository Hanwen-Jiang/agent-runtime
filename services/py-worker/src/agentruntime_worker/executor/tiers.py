"""Tier 的大小（附录 A、H.1：模型档@推理强度）。

N16-R：Planner 给单个 sub_agent 写的 tier 只能往小调，不大于子 Agent 的默认档（docs/plan/sub-agent.md 第 4 节）。
先比模型档，再比推理强度；认不出的档或强度不算小。
"""

from __future__ import annotations

# 只读研究的子 Agent 默认用中模型（附录 F）；AR_SUB_AGENT_POLICY=rules 时才用。
SUB_AGENT_TIER = "mid@low"
TIER_CLASSES = ("small", "mid", "strong")
TIER_EFFORTS = ("low", "medium", "high")


def tier_rank(tier: str) -> tuple[int, int] | None:
    """class@effort 的大小；认不出的档或强度是 None。"""
    cls, _, effort = str(tier).partition("@")
    if cls not in TIER_CLASSES or effort not in TIER_EFFORTS:
        return None
    return TIER_CLASSES.index(cls), TIER_EFFORTS.index(effort)


def at_most(tier: str, ceiling: str) -> bool:
    """tier 认得出且不大于 ceiling。"""
    rank, top = tier_rank(tier), tier_rank(ceiling)
    return rank is not None and top is not None and rank <= top


def sub_agent_tier(default: str, node_tier: str | None) -> str:
    """子 Agent 用的 Tier：Node 写了不大于 default 的 tier 时用它，否则用 default（不往大调，认不出的不用）。"""
    return str(node_tier) if node_tier and at_most(str(node_tier), default) else default
