"""Planner 的 Session 上下文（设计书 3.2.1：Planner 的输入是 Task State、Session 上下文、检索到的经验与 Recipe）。

由 PlanningContextSource 提供：设了 Registry 时是 registry.RegistryContextSource（Node Catalog、Recipe 与可用的 Tier），
开发与测试用 StaticContextSource。Session 之前的对话由 TranscriptContextSource 加上（TMP 计划第 11.13 节）。Memory Service 还没有接进来。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Protocol

from agentruntime_worker.activities.types import PlanRoundInput
from agentruntime_worker.session.transcript import TranscriptSource, Turn


STUB_TEMPLATE_PREFIX = "stub_"
"""桩 Planner 专用的 Node Template 名前缀（deploy/dev/registry-seed.json 的 stub_answer、stub_confirm）：真 Planner 的 Node Catalog
不列它们（2026-10-28 真模型实验第 3.1 条）；Stage Runner 解析模板不受影响。"""


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
    """Node Catalog 的视图（附录 D.7）：Node Template → (executor, effect)，Recipe → 任务族，工具 → effect；
    另有可用的 Tier（附录 H.1：能生成的 Model Profile 的 <class>@<tier 名>，排好序）。"""

    templates: dict[str, tuple[str, str]] = field(default_factory=dict)
    recipes: dict[str, str] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)
    tiers: tuple[str, ...] = ()
    """为空表示不知道有哪些 Tier：规划结果里的 tier 一律去掉，由 Model Gateway 按 role 选默认 Tier（附录 H.3）。"""
    skills: dict[str, str] = field(default_factory=dict)
    """Task 固定的 Skill（N-14）：名字 → 说明（用户写的，是数据）。正文不在这里，tool_loop 经 skill_read 读。"""


@dataclass(frozen=True)
class TaskFamily:
    """任务族词表里的一族（task_families.schema.json，TMP 计划第 6 节）。"""

    id: str
    description: str
    examples: tuple[str, ...] = ()


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
    task_families: tuple[TaskFamily, ...] | None = None
    """租户的任务族词表（Registry；TMP 计划第 6 节）。有时任务族只能从里面选；None 是还没有词表，照旧取分诊给的。"""
    conversation: tuple[Turn, ...] = ()
    """同一 Session 里之前的对话，从早到晚（TMP 计划第 11.13 节）。"""
    experiences: tuple[str, ...] = ()
    """Memory Service 检索到的经验（docs/plan/memory-live.md）：是数据，不是指令。"""
    personal_instructions: str = ""
    """Task 固定的个人指令，仅作为用户级偏好进入上下文。"""
    has_collections: bool = False
    image_messages: tuple[Any, ...] = ()
    """Authorized current-task images, carried separately from text summaries."""
    has_workspace: bool = False
    """这个 Session 有工作区：要读文件才答得好的消息不能走单次调用。"""


class PlanningContextSource(Protocol):
    async def load(self, inp: PlanRoundInput) -> PlanningContext: ...


class StaticContextSource:
    """开发与测试用：每个租户一份固定的上下文；没登记的租户用 default。"""

    def __init__(self, default: PlanningContext, by_tenant: dict[str, PlanningContext] | None = None) -> None:
        self.default = default
        self.by_tenant = dict(by_tenant or {})

    async def load(self, inp: PlanRoundInput) -> PlanningContext:
        return self.by_tenant.get(inp.tenant.tenant_id, self.default)


class TranscriptContextSource:
    """在 inner 给出的规划上下文上加上同一 Session 里之前的对话与有没有工作区（TMP 计划第 11.13 节）。"""

    def __init__(self, inner: PlanningContextSource, transcript: TranscriptSource) -> None:
        self.inner = inner
        self.transcript = transcript

    async def load(self, inp: PlanRoundInput) -> PlanningContext:
        ctx = await self.inner.load(inp)
        return replace(ctx, conversation=await self.transcript.load(inp.tenant, inp.task_id), has_workspace=bool(inp.tenant.workspace_id))
