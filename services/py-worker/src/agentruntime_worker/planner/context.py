"""Planner 的 Session 上下文（设计书 3.2.1：Planner 的输入是 Task State、Session 上下文、检索到的经验与 Recipe）。

Registry（Recipe 缓存、Node Catalog）与 Memory Service 还没有实现，先由 PlanningContextSource 提供，
开发与测试用 StaticContextSource。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from agentruntime_worker.activities.types import PlanRoundInput


@dataclass(frozen=True)
class RecipeEntry:
    """Recipe 缓存的一条（附录 C.4）：按任务族索引，记下风险等级、最近是否失败与典型成本。"""

    recipe: str
    task_family: str
    risk: str = "read"
    recent_failure: bool = False
    typical_cost_usd: float | None = None


@dataclass(frozen=True)
class CatalogView:
    """Node Catalog 的视图（附录 D.7）：Node Template → (executor, effect)，Recipe → 任务族，工具 → effect。"""

    templates: dict[str, tuple[str, str]] = field(default_factory=dict)
    recipes: dict[str, str] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanningContext:
    """规划时用到的 Session 上下文。"""

    risk_authorized: str
    """本 Session 已授权的最高风险等级（附录 C.1：风险按它比较）。"""
    effect_ceiling: str
    """租户与 Task 的 effect 上限。"""
    catalog: CatalogView
    recipes: list[RecipeEntry] = field(default_factory=list)
    session_budget_remaining_usd: float | None = None
    model_profile_updated: bool = False
    """相关 Model Profile 最近更新过（附录 C.2：缓存的 Recipe 不再可信）。"""


class PlanningContextSource(Protocol):
    async def load(self, inp: PlanRoundInput) -> PlanningContext: ...


class StaticContextSource:
    """开发与测试用：每个租户一份固定的上下文；没登记的租户用 default。"""

    def __init__(self, default: PlanningContext, by_tenant: dict[str, PlanningContext] | None = None) -> None:
        self.default = default
        self.by_tenant = dict(by_tenant or {})

    async def load(self, inp: PlanRoundInput) -> PlanningContext:
        return self.by_tenant.get(inp.tenant.tenant_id, self.default)
