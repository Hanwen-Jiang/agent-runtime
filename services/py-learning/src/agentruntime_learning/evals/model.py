"""被测对象的接口：Planner 与 Reviewer 的输入、输出（计划 S-08）。

Planner 的输入是 Round 开头能看到的东西：用户消息、当前 Task State 与 Session 上下文（Recipe 缓存、风险、effect 上限）。
Reviewer 的输入只有 Stage、Stage 结果、Task State（含验收条件 done_when）与之前各 Round 的摘要，
不含 Planner 的规划过程（设计书 3.2.1 第 4 条）：ReviewerInput 里就没有这样的字段。
两边都不给用例 id，被测对象不能按 id 查标注。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Usage:
    """一次模型调用的计量：Tier（形如 strong@medium）、供应商与成本。"""

    tier: str
    vendor: str
    cost_usd: float = 0.0


@dataclass(frozen=True)
class TemplateEntry:
    executor: str
    effect: str


@dataclass(frozen=True)
class Catalog:
    """评测用的 Node Catalog：Node Template（→ executor、effect）、Recipe（→ 任务族）与工具（→ effect）。"""

    templates: dict[str, TemplateEntry] = field(default_factory=dict)
    recipes: dict[str, str] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PlannerInput:
    message: str
    task_state: dict[str, Any] | None
    attachments: list[str]
    context: dict[str, Any]
    catalog: Catalog
    last_round: dict[str, Any] | None = None
    """上一轮：{"stage", "results", "verdict"}；replan 时 verdict.replan.keep_nodes 是要保留的 Node（S-09）。"""


@dataclass
class PlannerOutput:
    """Round 开头的产出：Triage 级别、Stage，以及 full、light、replan 时的 Task Plan。"""

    triage_level: str
    stage: dict[str, Any]
    task_plan: dict[str, Any] | None = None
    usage: list[Usage] = field(default_factory=list)


@dataclass(frozen=True)
class ReviewerInput:
    task_state: dict[str, Any]
    stage: dict[str, Any]
    results: dict[str, dict[str, Any]]
    history: list[dict[str, Any]]


@dataclass
class ReviewerOutput:
    verdict: dict[str, Any]
    usage: list[Usage] = field(default_factory=list)


class Subject(Protocol):
    """被测对象。name 写进报告，用来找上一版的报告做比较。"""

    name: str

    async def plan(self, inp: PlannerInput) -> PlannerOutput: ...

    async def review(self, inp: ReviewerInput) -> ReviewerOutput: ...
