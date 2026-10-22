"""Planner 的 decision activity（plan_round）。Worker 配置 AR_PLANNER=model 时代替桩 Planner。"""

from __future__ import annotations

import dataclasses

from temporalio import activity

from agentruntime_worker import names
from agentruntime_worker.executor.dev import model_gateway
from agentruntime_worker.activities.types import PlanRoundInput, PlanRoundOutput
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.planner.context import CatalogView, PlanningContext, PlanningContextSource, StaticContextSource
from agentruntime_worker.planner.planner import Planner, PlanRequest
from agentruntime_worker.registry import RegistryContextSource, registry_client


class PlannerActivities:
    def __init__(self, planner: Planner, contexts: PlanningContextSource) -> None:
        self.planner = planner
        self.contexts = contexts

    @activity.defn(name=names.PLAN_ROUND)
    async def plan_round(self, inp: PlanRoundInput) -> PlanRoundOutput:
        context = await self.contexts.load(inp)
        result = await self.planner.plan(
            PlanRequest(
                tenant=inp.tenant,
                task_id=inp.task_id,
                round_seq=inp.round_seq,
                message=inp.message,
                task_state=inp.task_state,
                attachments=list(inp.attachments),
                context=context,
                task_plan=inp.task_plan,
                last_round=inp.last_round,
            )
        )
        # 判定依据与模型调用写入日志（trace），不传给下游。
        activity.logger.info(
            "Planner：Triage %s，依据 %s；模型调用 %s%s",
            result.triage_level,
            "；".join(result.decision.reasons),
            [(c.purpose, c.tier, c.vendor) for c in result.calls],
            "；两次都不合格，用了保守规划" if result.fallback else "",
        )
        return PlanRoundOutput(stage=result.stage, triage_level=result.triage_level, task_plan=result.task_plan)

    def activities(self) -> dict[str, object]:
        return {names.PLAN_ROUND: self.plan_round}


def dev_planner(config: WorkerConfig) -> PlannerActivities:
    """AR_PLANNER=model 时的开发组装：真正的 Planner 经 model_gateway(config)（设了 AR_MODEL_GATEWAY_ADDRESS 时是真模型，否则是桩模型）。

    设了 AR_REGISTRY_ADDRESS 时 Node Catalog 与 Recipe 取自 Registry（T-08），否则用固定的上下文。没接 Model Gateway 时
    桩模型答不出合格的分诊与规划，Planner 会按 full 规划并回退到保守规划；真模型上的跑分见 py-learning 的评测（S-08、S-09）。
    """
    catalog = CatalogView(templates={"answer@v1": ("model", "none"), "rewrite@v1": ("model", "none")})
    defaults = PlanningContext(risk_authorized="read", effect_ceiling="read", catalog=catalog)
    contexts: PlanningContextSource = StaticContextSource(defaults)
    if config.registry_address:
        contexts = RegistryContextSource(registry_client(config), defaults=dataclasses.replace(defaults, catalog=CatalogView()))
    return PlannerActivities(Planner(model_gateway(config)), contexts)
