"""S-09 测试共用：按脚本作答的假 Model Gateway、Node Catalog 与规划请求。"""

from __future__ import annotations

import asyncio
from typing import Any

from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import enums_pb2, metering_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.planner.context import CatalogView, PlanningContext, RecipeEntry
from agentruntime_worker.planner.planner import PlanRequest

TASK_ID = "tsk_01J" + "0" * 22 + "P"
SESSION_ID = "ses_01J" + "0" * 22 + "S"
TENANT = Tenant(tenant_id="t1", user_id="u1", session_id=SESSION_ID)

CATALOG = CatalogView(
    templates={
        "answer@v1": ("model", "none"),
        "rewrite@v1": ("model", "none"),
        "summarize@v1": ("model", "none"),
        "sql_query@v1": ("program", "read"),
        "run_tests@v1": ("program", "read"),
        "clarify_scope@v1": ("human", "none"),
        "commit_progress@v1": ("program", "write_reversible"),
        "book_and_pay@v1": ("program", "write_irreversible"),
    },
    recipes={"ci_fix@v2": "engineering.ci_fix", "flight_booking@v1": "travel.booking", "refund_analysis@v3": "analytics.attribution"},
    tools={"web_search": "read", "repo_read": "read", "repo_edit": "write_reversible", "card_charge": "write_irreversible"},
)

CI_RECIPE = RecipeEntry(recipe="ci_fix@v2", task_family="engineering.ci_fix", risk="write_reversible", typical_cost_usd=0.5)
FLIGHT_RECIPE = RecipeEntry(recipe="flight_booking@v1", task_family="travel.booking", risk="write_irreversible", typical_cost_usd=0.4)


def context(*, risk: str = "read", ceiling: str = "write_irreversible", recipes: list[RecipeEntry] | None = None, budget: float | None = None, updated: bool = False) -> PlanningContext:
    return PlanningContext(risk_authorized=risk, effect_ceiling=ceiling, catalog=CATALOG, recipes=list(recipes or []), session_budget_remaining_usd=budget, model_profile_updated=updated)


def request(message: str = "分析上季度退款率上升的原因", *, ctx: PlanningContext | None = None, round_seq: int = 1, task_state: dict[str, Any] | None = None, task_plan: dict[str, Any] | None = None, last_round: dict[str, Any] | None = None) -> PlanRequest:
    return PlanRequest(tenant=TENANT, task_id=TASK_ID, round_seq=round_seq, message=message, task_state=task_state, attachments=[], context=ctx or context(), task_plan=task_plan, last_round=last_round)


def signals(kind: str = "new_task", *, family: str | None = "analytics.attribution", confidence: float = 0.95, effect: str = "read", explicit: bool = True) -> dict[str, Any]:
    return {"kind": kind, "task_family": family, "confidence": confidence, "effect": effect, "irreversible_params_explicit": explicit}


# N-04：第一次完整规划要给出的整体步骤（3～8 步，最多一步 active）。
PLAN_STEPS = [
    {"id": "fetch_data", "title": "取数", "status": "active", "round": 1},
    {"id": "analyze", "title": "分析原因", "status": "pending", "round": 1},
    {"id": "report", "title": "写出结论", "status": "pending", "round": 1},
]


def plan_answer(
    *nodes: dict[str, Any], level: str = "full", horizon: str = "per_stage", goal: str = "取数", seq: int = 7, steps: list[dict[str, Any]] | None = PLAN_STEPS,
) -> dict[str, Any]:
    """模型的规划输出：Task Plan 与 Stage。seq 故意写错，代码应改成本轮序号。steps 为 None 时不带整体步骤。"""
    stage_nodes = list(nodes) or [{"id": "q_region", "kind": "atomic", "template": "sql_query@v1", "executor": "program", "effect": "read", "role": "fetch"}]
    task_plan: dict[str, Any] = {"triage": {"level": level, "task_family": "analytics.attribution", "confidence": 0.7}, "horizon": horizon}
    if steps is not None:
        task_plan["steps"] = [dict(s) for s in steps]
    return {
        "task_plan": task_plan,
        "stage": {"seq": seq, "goal": goal, "exit_when": "数据齐全", "nodes": stage_nodes},
    }


class FakeGateway:
    """假 Model Gateway：按 CallContext.node_id 分派。

    triage：一个结果（dict）、一个异常，或按请求返回结果的函数。
    plan：依次返回的结果列表（dict 是结构化输出；str 当作不合格的文本回答；异常就抛出）。
    """

    def __init__(
        self,
        *,
        triage: Any = None,
        plan: list[Any] | None = None,
        vendor: str = "vendor_a",
        cost_micros: dict[str, int] | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.triage = triage if triage is not None else signals()
        self.plan = list(plan or [])
        self.vendor = vendor
        self.cost_micros = cost_micros or {"triage": 200, "plan": 20_000}
        self.delay_s = delay_s
        self.requests: list[mg.GenerateRequest] = []
        self.model_seconds = 0.0

    def purpose(self, request: mg.GenerateRequest) -> str:
        return "triage" if request.call.node_id.startswith("triage") else "plan"

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        self.requests.append(request)
        purpose = self.purpose(request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
            self.model_seconds += self.delay_s
        if purpose == "triage":
            answer = self.triage(request) if callable(self.triage) and not isinstance(self.triage, BaseException) else self.triage
        else:
            if not self.plan:
                raise AssertionError("脚本里没有更多的规划回答")
            answer = self.plan.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        response = mg.GenerateResponse(
            finish_reason=mg.FINISH_REASON_STOP,
            model=mg.ModelChoice(model_profile_id=f"{self.vendor}/{request.call.tier}", tier=request.call.tier, vendor=self.vendor),
            usage=metering_pb2.Usage(cost_usd_micros=self.cost_micros[purpose]),
        )
        if isinstance(answer, str):
            response.text = answer
        else:
            value = struct_pb2.Value()
            json_format.ParseDict(answer, value)
            response.result.CopyFrom(value)
        return response

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        raise AssertionError("Planner 不做嵌入")

    def calls(self, purpose: str) -> list[mg.GenerateRequest]:
        return [r for r in self.requests if self.purpose(r) == purpose]


def prompt_text(request: mg.GenerateRequest) -> str:
    return "\n".join(m.text for m in request.generation.messages)


def last_round(verdict: dict[str, Any], *, stage: dict[str, Any] | None = None, results: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "stage": stage or {"seq": 1, "goal": "读 CI 日志并修复", "exit_when": "测试通过", "nodes": [
            {"id": "read_log", "kind": "atomic", "template": "run_tests@v1", "executor": "program", "effect": "read", "role": "fetch"},
        ]},
        "results": results or {"read_log": {"role": "fetch", "status": "succeeded", "result": {"locale": "C"}}},
        "verdict": verdict,
    }


def role(name: str) -> int:
    return getattr(enums_pb2, "ROLE_" + name.upper())

