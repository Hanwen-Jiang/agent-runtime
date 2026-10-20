"""把 Worker 的 Planner（S-09）接进评测：python -m agentruntime_learning.evals run --subject agentruntime_learning.evals.worker_subject:planner_subject

- plan：S-08 的 PlannerInput 转成 Planner 的规划请求（评测专用租户 evals；Round 序号按 Task State 里已有的 Round 推出），
  模型调用的计量转成 Usage。
- review：S-10 之前用基线的 Reviewer；S-09 的门槛只看 Planner 的指标。

真模型经 Model Gateway 调用：环境变量 AR_EVAL_GATEWAY_FACTORY 给出 <模块>:<工厂函数>，工厂函数不带参数、返回
ModelGateway。没设时 planner_subject 报错，不悄悄换成假模型。
"""

from __future__ import annotations

import importlib
import os

from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.executor.model_gateway import ModelGateway
from agentruntime_worker.planner.context import CatalogView, PlanningContext, RecipeEntry
from agentruntime_worker.planner.planner import Planner, PlannerConfig, PlanRequest

from agentruntime_learning.evals import stages
from agentruntime_learning.evals.model import PlannerInput, PlannerOutput, ReviewerInput, ReviewerOutput, Subject, Usage
from agentruntime_learning.evals.subjects import BaselineSubject

ENV_GATEWAY_FACTORY = "AR_EVAL_GATEWAY_FACTORY"
EVAL_TENANT = "evals"


def _context(inp: PlannerInput) -> PlanningContext:
    ctx = inp.context
    catalog = CatalogView(
        templates={name: (entry.executor, entry.effect) for name, entry in inp.catalog.templates.items()},
        recipes=dict(inp.catalog.recipes),
        tools=dict(inp.catalog.tools),
    )
    recipes = [
        RecipeEntry(recipe=r["recipe"], task_family=r["task_family"], risk=r.get("risk", "read"), recent_failure=bool(r.get("recent_failure", False)))
        for r in ctx.get("recipes", [])
    ]
    return PlanningContext(
        risk_authorized=ctx["risk_before"],
        effect_ceiling=ctx["effect_ceiling"],
        catalog=catalog,
        recipes=recipes,
        session_budget_remaining_usd=ctx.get("session_budget_remaining_usd"),
        model_profile_updated=bool(ctx.get("model_profile_updated", False)),
    )


class WorkerPlannerSubject:
    name = "worker-planner"

    def __init__(self, gateway: ModelGateway, config: PlannerConfig | None = None) -> None:
        self.planner = Planner(gateway, config)
        self.reviewer = BaselineSubject()

    async def plan(self, inp: PlannerInput) -> PlannerOutput:
        state = inp.task_state or {}
        result = await self.planner.plan(
            PlanRequest(
                tenant=Tenant(tenant_id=EVAL_TENANT, session_id=str(state.get("session_id", ""))),
                task_id=str(state.get("task_id") or stages.EVAL_TASK_ID),
                round_seq=len(state.get("rounds") or []) + 1,
                message=inp.message,
                task_state=inp.task_state,
                attachments=list(inp.attachments),
                context=_context(inp),
                last_round=inp.last_round,
            )
        )
        return PlannerOutput(
            triage_level=result.triage_level,
            stage=result.stage,
            task_plan=result.task_plan,
            usage=[Usage(tier=c.tier, vendor=c.vendor, cost_usd=c.cost_usd) for c in result.calls],
        )

    async def review(self, inp: ReviewerInput) -> ReviewerOutput:
        return await self.reviewer.review(inp)


def planner_subject() -> Subject:
    """命令行用的工厂：按 AR_EVAL_GATEWAY_FACTORY 拿到 Model Gateway。"""
    spec = os.environ.get(ENV_GATEWAY_FACTORY, "")
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(f"要用真模型评测 Planner，请设 {ENV_GATEWAY_FACTORY}=<模块>:<工厂函数>（工厂函数返回 Model Gateway）")
    gateway = getattr(importlib.import_module(module_name), attr)()
    return WorkerPlannerSubject(gateway)
