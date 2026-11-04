"""Planner 的 decision activity（plan_round）。Worker 配置 AR_PLANNER=model 时代替桩 Planner。

规划之后，每个 tool_loop 的 Goal Node 加上 context_read（R-03 评审 D11）：模型按路径取回 Context Builder 外置或遮蔽的内容。
只在 context_read 已登记、且这个 Goal 的 effect 上限（effect_ceiling，没写时取 Task 的）允许 read 时加；在 activity 里加，
不改 Stage Runner。开发组装下 Node Catalog 列出 ToolDriver 登记的全部工具（ToolsContextSource，含 context_read），
Planner 自己写了 context_read 时不再重复加。
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Any

from temporalio import activity

from agentruntime_worker import names
from agentruntime_worker.executor.dev import model_gateway
from agentruntime_worker.activities.types import PlanRoundInput, PlanRoundOutput, Tenant
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.executor.heartbeat import heartbeating
from agentruntime_worker.planner.context import CatalogView, PlanningContext, PlanningContextSource, StaticContextSource, TranscriptContextSource
from agentruntime_worker.planner.planner import Planner, PlannerConfig, PlanRequest
from agentruntime_worker.registry import RegistryContextSource, registry_client
from agentruntime_worker.session.transcript import worker_transcript
from agentruntime_worker.tools import CONTEXT_READ, ToolCatalog, dev_tools
from agentruntime_worker.workflows import stage_plan as sp


class PlannerActivities:
    def __init__(self, planner: Planner, contexts: PlanningContextSource, *, tools: ToolCatalog | None = None) -> None:
        self.planner = planner
        self.contexts = contexts
        self.tools = tools

    @activity.defn(name=names.PLAN_ROUND)
    async def plan_round(self, inp: PlanRoundInput) -> PlanRoundOutput:
        async with heartbeating():
            return await self._plan_round(inp)

    async def _plan_round(self, inp: PlanRoundInput) -> PlanRoundOutput:
        context = await self.contexts.load(inp)
        request = PlanRequest(
            tenant=inp.tenant,
            task_id=inp.task_id,
            round_seq=inp.round_seq,
            message=inp.message,
            task_state=inp.task_state,
            attachments=list(inp.attachments),
            context=context,
            task_plan=inp.task_plan,
            last_round=inp.last_round,
            earlier=inp.earlier_results,
        )
        # 工作流里的决策重试用完时（TMP 计划第 4 节）不调模型，直接给保守规划。
        result = self.planner.plan_without_model(request) if inp.no_model else await self.planner.plan(request)
        # 判定依据与模型调用写入日志（trace），不传给下游。
        activity.logger.info(
            "Planner：Triage %s，依据 %s；模型调用 %s%s%s",
            result.triage_level,
            "；".join(result.decision.reasons),
            [(c.purpose, c.tier, c.vendor) for c in result.calls],
            "；两次都不合格，用了保守规划" if result.fallback else "",
            f"；代码改正：{'；'.join(result.adjustments)}" if result.adjustments else "",
        )
        stage = await self._with_context_read(inp.tenant, result.stage, context.effect_ceiling)
        cost = round(sum(c.cost_usd for c in result.calls), 6)
        return PlanRoundOutput(
            stage=stage, triage_level=result.triage_level, task_plan=result.task_plan, cost_usd=cost, goal_code=result.goal_code, task_family=result.task_family,
            exit_when_code=result.exit_when_code, params=result.params,
        )

    async def _with_context_read(self, tenant: Tenant, stage: Any, ceiling: str) -> Any:
        """tool_loop 的 Goal Node 加上 context_read（已有的工具不变，追加在后面）。"""
        if self.tools is None or not isinstance(stage, dict) or not isinstance(stage.get("nodes"), list):
            return stage
        if (await self.tools.effects(tenant, [CONTEXT_READ])).get(CONTEXT_READ) != "read":
            return stage
        out = copy.deepcopy(stage)
        for node in out["nodes"]:
            if not isinstance(node, dict) or node.get("kind") != "goal" or node.get("mode") != "tool_loop":
                continue
            allowed = str(node.get("effect_ceiling") or ceiling)
            if allowed not in sp.EFFECT_ORDER or sp.effect_rank(allowed) < sp.effect_rank("read"):
                continue
            tools = [str(t) for t in node.get("tools") or []]
            if CONTEXT_READ not in tools:
                node["tools"] = [*tools, CONTEXT_READ]
        return out

    def activities(self) -> dict[str, object]:
        return {names.PLAN_ROUND: self.plan_round}


class ToolsContextSource:
    """在 inner 给出的规划上下文上加上 ToolDriver 登记的工具与 effect（T-13：之前 Node Catalog 里没有工具，真 Planner 规划不出
    用平台内工具的 tool_loop），effect 上限跟 Task State 的 constraints（task-service 按租户策略写入；Registry 的上下文本来就这样）。"""

    def __init__(self, inner: PlanningContextSource, tools: ToolCatalog) -> None:
        self.inner = inner
        self.tools = tools

    async def load(self, inp: PlanRoundInput) -> PlanningContext:
        ctx = await self.inner.load(inp)
        tools = {**ctx.catalog.tools, **await self.tools.all_effects(inp.tenant)}
        ceiling = (inp.task_state.get("constraints") or {}).get("effect_ceiling") or ctx.effect_ceiling
        return dataclasses.replace(ctx, catalog=dataclasses.replace(ctx.catalog, tools=tools), effect_ceiling=str(ceiling))


def dev_planner(config: WorkerConfig) -> PlannerActivities:
    """AR_PLANNER=model 时的开发组装：真正的 Planner 经 model_gateway(config)（设了 AR_MODEL_GATEWAY_ADDRESS 时是真模型，否则是桩模型）。

    设了 AR_REGISTRY_ADDRESS 时 Node Catalog 与 Recipe 取自 Registry（T-08），否则用固定的上下文；两种都加上开发用 ToolDriver
    登记的工具（与 Node Executor 的 call_tool、tool_loop 给模型的工具定义是同一份登记）。没接 Model Gateway 时
    桩模型答不出合格的分诊与规划，Planner 会按 full 规划并回退到保守规划；真模型上的跑分见 py-learning 的评测（S-09）。
    """
    catalog = CatalogView(templates={"answer@v1": ("model", "none"), "rewrite@v1": ("model", "none")})
    defaults = PlanningContext(risk_authorized="read", effect_ceiling="read", catalog=catalog)
    inner: PlanningContextSource = StaticContextSource(defaults)
    if config.registry_address:
        inner = RegistryContextSource(registry_client(config), defaults=dataclasses.replace(defaults, catalog=CatalogView()))
    tools = ToolCatalog(dev_tools(config))
    source: PlanningContextSource = ToolsContextSource(inner, tools)
    transcript = worker_transcript(config)
    if transcript is not None:
        source = TranscriptContextSource(source, transcript)
    from agentruntime_worker.memory import RetrievalContextSource, searcher

    memory = searcher(config)
    if memory is not None:
        source = RetrievalContextSource(source, memory)
    # N16-R：派子 Agent 的规矩（AR_SUB_AGENT_POLICY=rules，默认 off）。
    planner = Planner(model_gateway(config), PlannerConfig(sub_agent_rules=config.sub_agent_policy == "rules"))
    return PlannerActivities(planner, source, tools=tools)
