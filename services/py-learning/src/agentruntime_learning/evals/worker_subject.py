"""把 Worker 的 Planner（S-09）与 Reviewer（S-10）接进评测：

    python -m agentruntime_learning.evals run --subject agentruntime_learning.evals.worker_subject:worker_subject

- plan：S-08 的 PlannerInput 转成 Planner 的规划请求（评测专用租户 evals；Round 序号按 Task State 里已有的 Round 推出），
  模型调用的计量转成 Usage。
- review：worker_subject 用 Worker 的 Reviewer（S-10）：ReviewerInput 转成评审请求，reroute 按评测集 Catalog 的 Recipe
  校验（风险取用例里 Recipe 缓存记的最高风险，没记的按 read）；planner_subject 仍用基线的 Reviewer（S-09 的门槛只看 Planner）。

真模型经 Model Gateway 调用：环境变量 AR_EVAL_GATEWAY_FACTORY 给出 <模块>:<工厂函数>，工厂函数不带参数、返回
ModelGateway。没设时两个工厂都报错，不悄悄换成假模型。

Planner 与 Reviewer 会吞掉模型调用的错误，回退到保守规划或规则判定（生产上就该这样）；评测时这会让分数不再代表模型的
判断（2026-10-22 实测：供应商额度用完后每次调用都 403，评测照样“跑完”）。所以被测对象把 Gateway 包一层 CountingGateway，
记下失败的次数与 code，运行器写进报告（metrics.model_call_failures）。

TMP 计划第 8 节：CountingGateway 还记下每个 role 成功调用用到的 model_profile_id（报告的 models），plan、review 的输出带上
Planner、Reviewer 是否回退（fallback），运行器据此算回退率、判这次运行是否有效。
"""

from __future__ import annotations

import importlib
import os
from collections import Counter
from typing import Any

from agentruntime.common.v1 import enums_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.executor.model_gateway import ModelGateway
from agentruntime_worker.planner.context import CatalogView, PlanningContext, RecipeEntry
from agentruntime_worker.planner.planner import Planner, PlannerConfig, PlanRequest
from agentruntime_worker.reviewer.reviewer import Reviewer, ReviewerConfig, ReviewRequest
from agentruntime_worker.reviewer.rules import ReviewCatalog
from agentruntime_worker.session.transcript import Turn

from agentruntime_learning.evals import stages
from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET, EvalSet, load_eval_set
from agentruntime_learning.evals.context import ContextSubject, GatewayContextSubject
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
        conversation=tuple(Turn(t["message"], t["reply"], t["status"]) for t in ctx.get("conversation", [])),
        has_workspace=bool(ctx.get("has_workspace", False)),
    )


def role_name(role: int) -> str:
    """CallContext.role 的小写名（route、plan、judge……），报告的 models 按它分。"""
    try:
        return enums_pb2.Role.Name(role).removeprefix("ROLE_").lower()
    except ValueError:
        return str(role)


class CountingGateway:
    """包一层 Model Gateway，记下失败的调用（次数与 code）与成功调用用到的模型（role、model_profile_id），错误照样抛给调用方。"""

    def __init__(self, inner: ModelGateway) -> None:
        self.inner = inner
        self.failures: Counter[str] = Counter()
        self.models: Counter[tuple[str, str]] = Counter()

    async def _call(self, method: str, request: Any) -> Any:
        try:
            response = await getattr(self.inner, method)(request)
        except Exception as exc:
            self.failures[str(getattr(exc, "code", "") or type(exc).__name__)] += 1
            raise
        profile = response.model.model_profile_id if response.HasField("model") else ""
        self.models[(role_name(request.call.role), profile or "unknown")] += 1
        return response

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        return await self._call("generate", request)

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        return await self._call("embed", request)


class WorkerPlannerSubject:
    name = "worker-planner"

    def __init__(self, gateway: ModelGateway, config: PlannerConfig | None = None) -> None:
        self.gateway = CountingGateway(gateway)
        self.planner = Planner(self.gateway, config)
        self.reviewer = BaselineSubject()

    @property
    def model_call_failures(self) -> Counter[str]:
        """到目前为止失败的模型调用，按 code 计数（运行器按每次评测前后的差算本次的）。"""
        return Counter(self.gateway.failures)

    @property
    def model_calls(self) -> Counter[tuple[str, str]]:
        """到目前为止成功的模型调用，按 (role, model_profile_id) 计数（运行器同样按前后的差算本次的）。"""
        return Counter(self.gateway.models)

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
            fallback=result.fallback,
        )

    async def review(self, inp: ReviewerInput) -> ReviewerOutput:
        return await self.reviewer.review(inp)


def eval_recipes(es: EvalSet) -> dict[str, str]:
    """评测集 Catalog 里的 Recipe → 风险：取用例里 Recipe 缓存记的最高风险，没记的按 read。"""
    risks: dict[str, str] = {name: "read" for name in es.catalog.recipes}
    for case in es.task_cases:
        for entry in case.planner_input["context"].get("recipes", []):
            name, risk = entry["recipe"], entry.get("risk", "read")
            if name in risks and stages.rank(risk) > stages.rank(risks[name]):
                risks[name] = risk
    return risks


class WorkerSubject(WorkerPlannerSubject):
    """Planner 加 Reviewer（S-10）：整个 Task Loop 的决策都用 Worker 的实现。"""

    name = "worker"

    def __init__(
        self,
        gateway: ModelGateway,
        config: PlannerConfig | None = None,
        *,
        reviewer_config: ReviewerConfig | None = None,
        recipes: dict[str, str] | None = None,
    ) -> None:
        super().__init__(gateway, config)
        self.worker_reviewer = Reviewer(self.gateway, reviewer_config)
        self.recipes = recipes if recipes is not None else eval_recipes(load_eval_set(DEFAULT_EVAL_SET))

    async def review(self, inp: ReviewerInput) -> ReviewerOutput:
        state = inp.task_state
        ceiling = str((state.get("constraints") or {}).get("effect_ceiling") or "read")
        result = await self.worker_reviewer.review(
            ReviewRequest(
                tenant=Tenant(tenant_id=EVAL_TENANT, session_id=str(state.get("session_id", ""))),
                task_id=str(state.get("task_id") or stages.EVAL_TASK_ID),
                round_seq=max(len(state.get("rounds") or []), 1),
                task_state=state,
                stage=inp.stage,
                results=inp.results,
                history=list(inp.history),
                catalog=ReviewCatalog(recipes=dict(self.recipes), effect_ceiling=ceiling),
            )
        )
        return ReviewerOutput(
            verdict=result.verdict,
            usage=[Usage(tier=c.tier, vendor=c.vendor, cost_usd=c.cost_usd) for c in result.calls],
            fallback=result.fallback,
        )


def _gateway() -> ModelGateway:
    spec = os.environ.get(ENV_GATEWAY_FACTORY, "")
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(f"要用真模型评测 Worker 的 Planner 与 Reviewer，请设 {ENV_GATEWAY_FACTORY}=<模块>:<工厂函数>（工厂函数返回 Model Gateway）")
    return getattr(importlib.import_module(module_name), attr)()


def worker_subject() -> Subject:
    """命令行用的工厂：按 AR_EVAL_GATEWAY_FACTORY 拿到 Model Gateway，Planner 与 Reviewer 都用 Worker 的实现。"""
    return WorkerSubject(_gateway())


def planner_subject() -> Subject:
    """命令行用的工厂：按 AR_EVAL_GATEWAY_FACTORY 拿到 Model Gateway；Reviewer 用基线（S-09 的门槛只看 Planner）。"""
    return WorkerPlannerSubject(_gateway())


def context_subject() -> ContextSubject:
    """S-11 压缩评测的工厂（python -m agentruntime_learning.evals context --subject …:context_subject）：
    按 AR_EVAL_GATEWAY_FACTORY 拿到 Model Gateway；增量摘要、Compaction Probe、答题与下一步动作都经它。"""
    return GatewayContextSubject(_gateway())
