"""Planner（计划 S-09）：分诊与 Triage → 按级别产出 Stage 与 Task Plan。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import enums_pb2, payload_pb2, tenant_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.executor.model_gateway import ModelGateway, billed_cost_usd
from agentruntime_worker.planner import prompts, validate
from agentruntime_worker.planner.context import PlanningContext
from agentruntime_worker.planner.triage import SIGNALS_SCHEMA, UNCLASSIFIED_TASK_FAMILY, Signals, TriageDecision, decide, parse_signals, reviewer_decision
from agentruntime_worker.workflows import stage_plan as sp


@dataclass(frozen=True)
class PlannerConfig:
    classifier_tier: str = "small@low"
    """分诊用的小模型。"""
    planner_tier: str = "strong@medium"
    """full 与 replan 用的强模型（附录 C.3 的 tiers.planner）。"""
    confidence_threshold: float = 0.8
    """分诊置信度低于它就不走 light（附录 C.2）。"""
    rewrite_template: str = "rewrite@v1"
    """fast 续改用的 Node Template。"""
    answer_template: str = "answer@v1"
    """fast 作答、light 交付用的 Node Template。"""


@dataclass(frozen=True)
class PlanRequest:
    tenant: Tenant
    task_id: str
    round_seq: int
    message: str
    task_state: dict[str, Any] | None
    attachments: list[str]
    context: PlanningContext
    task_plan: dict[str, Any] | None = None
    """之前的轮次产出的 Task Plan。"""
    last_round: dict[str, Any] | None = None
    """上一轮：{"stage", "results", "verdict"}；replan 时 verdict.replan.keep_nodes 是要保留的 Node。"""


@dataclass(frozen=True)
class ModelCall:
    """一次模型调用的计量。purpose 为 triage 或 plan。"""

    purpose: str
    tier: str
    vendor: str
    cost_usd: float


@dataclass
class PlanResult:
    triage_level: str
    stage: dict[str, Any]
    task_plan: dict[str, Any] | None
    decision: TriageDecision
    calls: list[ModelCall] = field(default_factory=list)
    fallback: bool = False
    """两次都不合格，用了保守规划。"""
    problems: list[str] = field(default_factory=list)
    """最后一次不合格的说明（写入 trace）。"""
    adjustments: list[str] = field(default_factory=list)
    """代码替模型改正了什么（例如去掉不可用的 Tier；写入 trace）。"""
    task_family: str = ""
    """这一轮定的任务族（TMP 计划第 6 节）：有词表时由服务端定；fast 也有。"""
    goal_code: str = ""
    """goal 是代码给的固定说明时的代码（TMP 计划第 5 节）：fast 的 reply_to_message。"""


PLAN_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["task_plan", "stage"],
    "properties": {"task_plan": {"type": "object"}, "stage": {"type": "object"}},
}
"""规划的结构化输出外层；Stage 与 Task Plan 的完整 schema 放在 system 区（模型网关不解析 $ref）。"""


def _value(data: Any) -> struct_pb2.Value:
    value = struct_pb2.Value()
    json_format.ParseDict(data, value)
    return value


def _rank(effect: str) -> int:
    return sp.effect_rank(effect)


class Planner:
    def __init__(self, gateway: ModelGateway, config: PlannerConfig | None = None) -> None:
        self.gateway = gateway
        self.config = config or PlannerConfig()

    async def plan(self, request: PlanRequest) -> PlanResult:
        return _settle_family(request, await self._plan(request))

    async def _plan(self, request: PlanRequest) -> PlanResult:
        calls: list[ModelCall] = []
        decided_by_reviewer = reviewer_decision(request.last_round, context=request.context) is not None
        signals = None if decided_by_reviewer else await self._classify(request, calls)
        decision = decide(signals, context=request.context, last_round=request.last_round, confidence_threshold=self.config.confidence_threshold)
        if decision.level == "fast":
            stage = self._fast_stage(request, decision)
            if stage is not None:
                return PlanResult(triage_level="fast", stage=stage, task_plan=None, decision=decision, calls=calls)
            decision = replace(decision, level="full", reasons=[*decision.reasons, "fast 用的 Node Template 不在 Catalog 里，改走 full"])
        if decision.level == "light":
            light = self._light(request, decision)
            if light is not None:
                stage, task_plan = light
                return PlanResult(triage_level="light", stage=stage, task_plan=task_plan, decision=decision, calls=calls)
            decision = replace(decision, level="full", reasons=[*decision.reasons, "命中的 Recipe 展开不出合格的 Stage，改走 full"])
        return await self._plan_with_model(request, decision, calls)

    def plan_without_model(self, request: PlanRequest) -> PlanResult:
        """不调模型的保守规划：工作流里的决策重试用完或到了总时限时用（TMP 计划 eval-observability-and-runtime-fixes 第 4 节）。
        分诊按没有信号判定，fast、light 都改走 full；任务族没有就记 general.unclassified。"""
        decision = decide(None, context=request.context, last_round=request.last_round, confidence_threshold=self.config.confidence_threshold)
        if decision.level in ("fast", "light"):
            decision = replace(decision, level="full", reasons=[*decision.reasons, "模型不可用：改走保守规划"])
        stage, task_plan = self._fallback(request, decision, self._keep_nodes(request))
        result = PlanResult(triage_level=decision.level, stage=stage, task_plan=task_plan, decision=decision, calls=[], fallback=True, problems=["模型不可用：决策调用的重试用完"])
        return _settle_family(request, result)

    # --- 模型调用 ---

    def _tenant(self, request: PlanRequest) -> tenant_pb2.TenantContext:
        t = request.tenant
        return tenant_pb2.TenantContext(tenant_id=t.tenant_id, user_id=t.user_id, workspace_id=t.workspace_id, session_id=t.session_id)

    async def _call(self, request: PlanRequest, *, purpose: str, role: int, tier: str, messages: list[mg.ChatMessage], output_schema: dict[str, Any], calls: list[ModelCall]) -> mg.GenerateResponse:
        req = mg.GenerateRequest(
            tenant=self._tenant(request),
            call=mg.CallContext(task_id=request.task_id, node_id=purpose, role=role, tier=tier),
            generation=mg.GenerateParams(messages=messages, output_schema=payload_pb2.JsonPayload(value=_value(output_schema))),
        )
        try:
            response = await self.gateway.generate(req)
        except Exception as exc:
            # 失败之前已经计费的也算进成本（TMP 计划第 11.11 节）。
            if billed := billed_cost_usd(exc):
                calls.append(ModelCall(purpose=purpose, tier=tier, vendor="", cost_usd=billed))
            raise
        calls.append(ModelCall(purpose=purpose, tier=response.model.tier or tier, vendor=response.model.vendor, cost_usd=response.usage.cost_usd_micros / 1_000_000))
        return response

    @staticmethod
    def _structured(response: mg.GenerateResponse) -> Any:
        if response.HasField("result"):
            return json_format.MessageToDict(response.result)
        return None

    async def _classify(self, request: PlanRequest, calls: list[ModelCall]) -> Signals | None:
        try:
            response = await self._call(
                request,
                purpose="triage",
                role=enums_pb2.ROLE_ROUTE,
                tier=self.config.classifier_tier,
                messages=prompts.classifier_messages(request.message, request.task_state, request.context),
                output_schema=SIGNALS_SCHEMA,
                calls=calls,
            )
        except Exception:  # noqa: BLE001 — 分诊失败按 full 规划，不让 Task 失败
            return None
        return parse_signals(self._structured(response))

    # --- fast 与 light：不调用强模型 ---

    def _fast_stage(self, request: PlanRequest, decision: TriageDecision) -> dict[str, Any] | None:
        template = self.config.rewrite_template if decision.kind == "follow_up" else self.config.answer_template
        if template not in request.context.catalog.templates:
            return None
        # 以 $ 开头的输入是引用；用户消息照原样传，开头的 $ 要写成 $$（字面量）。
        text = "$" + request.message if request.message.startswith("$") else request.message
        node = {"id": "answer", "kind": "atomic", "template": template, "executor": "model", "output": "generate", "effect": "none", "role": "deliver", "inputs": {"request": text}}
        return {"seq": request.round_seq, "goal": "回复用户这条消息", "exit_when": "answer 给出回复", "nodes": [node]}

    def _light(self, request: PlanRequest, decision: TriageDecision) -> tuple[dict[str, Any], dict[str, Any]] | None:
        recipe = decision.recipe
        context = request.context
        if recipe is None or recipe.recipe not in context.catalog.recipes or self.config.answer_template not in context.catalog.templates:
            return None
        if _rank(recipe.risk) > _rank(context.effect_ceiling):
            return None
        stage = {
            "seq": request.round_seq,
            "goal": f"按 {recipe.recipe} 完成：{request.message}",
            "exit_when": "Recipe 的子图完成，结果已交付",
            "nodes": [
                {"id": "run_recipe", "kind": "goal", "goal": request.message, "done_when": f"按 {recipe.recipe} 完成", "mode": "template", "recipe": recipe.recipe, "effect_ceiling": recipe.risk, "output_schema": {"type": "object"}},
                {"id": "deliver", "kind": "atomic", "template": self.config.answer_template, "executor": "model", "output": "generate", "effect": "none", "role": "deliver", "after": ["run_recipe"]},
            ],
        }
        triage: dict[str, Any] = {"level": "light", "confidence": decision.confidence}
        if decision.task_family:
            triage["task_family"] = decision.task_family
        task_plan = {"triage": triage, "recipe": recipe.recipe, "horizon": "full_dag"}
        if validate.problems(stage, task_plan, level="light", context=context, keep_nodes=[]):
            return None
        return stage, task_plan

    # --- full 与 replan：一次强模型调用，不合格重问一次，再回退 ---

    def _keep_nodes(self, request: PlanRequest) -> list[str]:
        """不得重做的 Node：replan 时是 Reviewer 保留的；repair 换方法时是修复范围（要修的 Node 及其下游）之外、
        上一轮成功的 Node（S-10）。"""
        last = request.last_round or {}
        verdict = last.get("verdict") or {}
        if verdict.get("verdict") == "repair" and (verdict.get("repair") or {}).get("strategy") == "change_method":
            stage = last.get("stage") or {}
            repaired = set(verdict["repair"].get("nodes") or [])
            redo = repaired | sp.descendants(stage, repaired)
            results = last.get("results") or {}
            return sorted(str(n["id"]) for n in stage.get("nodes") or [] if n["id"] not in redo and (results.get(n["id"]) or {}).get("status") == "succeeded")
        return [str(n) for n in (verdict.get("replan") or {}).get("keep_nodes") or []]

    def _with_triage(self, task_plan: Any, decision: TriageDecision) -> Any:
        if not isinstance(task_plan, dict):
            return task_plan
        triage = dict(task_plan.get("triage") or {}) if isinstance(task_plan.get("triage"), dict) else {}
        triage["level"] = decision.level
        if "task_family" not in triage:
            triage["task_family"] = decision.task_family or UNCLASSIFIED_TASK_FAMILY
        return {**task_plan, "triage": triage}

    async def _plan_with_model(self, request: PlanRequest, decision: TriageDecision, calls: list[ModelCall]) -> PlanResult:
        keep = self._keep_nodes(request)
        messages = prompts.planning_messages(
            catalog=request.context.catalog,
            context=request.context,
            decision=decision,
            message=request.message,
            task_state=request.task_state,
            task_plan=request.task_plan,
            last_round=request.last_round,
            round_seq=request.round_seq,
            keep_nodes=keep,
        )
        found: list[str] = []
        for attempt in range(2):
            try:
                response = await self._call(request, purpose="plan", role=enums_pb2.ROLE_PLAN, tier=self.config.planner_tier, messages=messages, output_schema=PLAN_OUTPUT_SCHEMA, calls=calls)
            except Exception as exc:  # noqa: BLE001 — 模型出错算一次不合格
                found = [f"调用模型失败：{type(exc).__name__}"]
                answer_text = ""
            else:
                answer = self._structured(response)
                answer_text = response.text if answer is None else json.dumps(answer, ensure_ascii=False)
                if not isinstance(answer, dict):
                    found = ["回答不是 JSON 对象：要按输出 schema 给出 {\"task_plan\": …, \"stage\": …}"]
                else:
                    stage = validate.normalize_stage(answer.get("stage"), round_seq=request.round_seq, tenant_id=request.tenant.tenant_id, task_id=request.task_id)
                    task_plan = self._with_triage(answer.get("task_plan"), decision)
                    stage, task_plan, adjusted = validate.drop_unknown_tiers(stage, task_plan, request.context.catalog.tiers)
                    stage, capped = validate.cap_budgets(stage, validate.task_budget_remaining(request.task_state))
                    adjusted = [*adjusted, *capped]
                    found = validate.problems(stage, task_plan, level=decision.level, context=request.context, keep_nodes=keep)
                    if not found:
                        return PlanResult(triage_level=decision.level, stage=stage, task_plan=task_plan, decision=decision, calls=calls, adjustments=adjusted)
            if attempt == 0:
                if _budget_spent(request.task_state, calls):
                    # 美元预算已经用完：不再重问，用保守规划（TMP 计划第 11.11 节；第一次规划照常，按 11.4 不拦）。
                    found = [*found, "美元预算已经用完，不再重问"]
                    break
                messages = prompts.retry_messages(messages, answer_text, found)
        stage, task_plan = self._fallback(request, decision, keep)
        return PlanResult(triage_level=decision.level, stage=stage, task_plan=task_plan, decision=decision, calls=calls, fallback=True, problems=found)

    def _fallback(self, request: PlanRequest, decision: TriageDecision, keep: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
        """保守规划：一个 tool_loop 的 Goal Node，去探索并完成剩余部分；不重做要保留的 Node。"""
        node_id = next(i for i in ("explore", "explore_rest", "explore_next") if i not in keep)
        done_when = str((request.task_state or {}).get("done_when") or "完成用户的请求")
        goal = request.message if not keep else f"在已验证的 {'、'.join(keep)} 的基础上完成剩余部分：{request.message}"
        stage = {
            "seq": request.round_seq,
            "goal": "探索并推进这个任务",
            "exit_when": f"{node_id} 给出结果",
            "nodes": [{"id": node_id, "kind": "goal", "goal": goal, "done_when": done_when, "mode": "tool_loop", "output_schema": {"type": "object"}}],
        }
        triage: dict[str, Any] = {"level": decision.level, "task_family": decision.task_family or UNCLASSIFIED_TASK_FAMILY}
        return stage, {"triage": triage, "horizon": "explore"}


def _budget_spent(task_state: Any, calls: list[ModelCall]) -> bool:
    """Task 的美元预算是否已经用完：剩余（预算减去之前各 Round 的成本）再减去这次已调的（分诊与规划），不大于 0。没有预算时为 False。"""
    remaining = validate.task_budget_remaining(task_state)
    return remaining is not None and remaining - sum(c.cost_usd for c in calls) <= 0


FAST_GOAL_CODE = "reply_to_message"


def _settle_family(request: PlanRequest, result: PlanResult) -> PlanResult:
    """定这一轮的任务族与 goal_code（TMP 计划第 5、6 节）。有词表时任务族由服务端定：分诊给的在词表里就用它，否则记
    general.unclassified，Task Plan 里规划模型写的改成它；没有词表时照旧，取 Task Plan 里的，再没有就取分诊给的。"""
    vocabulary = request.context.task_families
    decided = result.decision.task_family
    task_plan = result.task_plan
    if vocabulary is not None:
        family = decided if decided and decided in {f.id for f in vocabulary} else UNCLASSIFIED_TASK_FAMILY
        if isinstance(task_plan, dict):
            triage = dict(task_plan.get("triage") or {}) if isinstance(task_plan.get("triage"), dict) else {}
            task_plan = {**task_plan, "triage": {**triage, "task_family": family}}
    else:
        written = ((task_plan or {}).get("triage") or {}).get("task_family") if isinstance(task_plan, dict) else None
        family = str(written or decided or UNCLASSIFIED_TASK_FAMILY)
    goal_code = FAST_GOAL_CODE if result.triage_level == "fast" else ""
    return replace(result, task_plan=task_plan, task_family=family, goal_code=goal_code)
