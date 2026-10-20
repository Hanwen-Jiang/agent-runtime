"""rubric 评分（计划 S-08）：用与被测模型不同家的模型打分。

judge 经 Model Gateway 调用：CallContext.role = judge，avoid_vendor 填被测对象用过的供应商（附录 H.3），
返回后再核对实际服务的供应商；与被测对象同家时抛 SameVendorError，不记分。
rubric 分数单独报告，不计入通过与否；没配 judge 时报告“未评”，不按 0 分算。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import enums_pb2, payload_pb2, tenant_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_worker.executor.model_gateway import ModelGateway


class SameVendorError(ValueError):
    """judge 与被测对象是同一家供应商。"""


@dataclass(frozen=True)
class RubricScore:
    score: float
    """0 到 1。"""
    vendor: str
    cost_usd: float = 0.0


class RubricJudge(Protocol):
    async def score(self, *, criteria: list[str], material: dict[str, Any], subject_vendors: frozenset[str]) -> RubricScore: ...


_SYSTEM = (
    "你是评测里的 judge。下面给出评分要点（rubric）和被评的材料（Planner 的 Task Plan 与 Stage，或 Reviewer 的判定）。"
    "逐条对照要点判断材料是否做到，按做到的程度给出 0 到 1 的总分，并用一两句话说明理由。只按要点打分，不因长短、措辞或先后给分。"
)
_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["score", "notes"],
    "properties": {"score": {"type": "number", "minimum": 0, "maximum": 1}, "notes": {"type": "string"}},
    "additionalProperties": False,
}


def _value(data: Any) -> struct_pb2.Value:
    value = struct_pb2.Value()
    json_format.ParseDict(data, value)
    return value


class GatewayRubricJudge:
    """经 Model Gateway 打分的 judge。tenant_id 是评测专用的租户。"""

    def __init__(self, gateway: ModelGateway, *, tenant_id: str, tier: str = "strong@medium") -> None:
        self.gateway = gateway
        self.tenant_id = tenant_id
        self.tier = tier

    async def score(self, *, criteria: list[str], material: dict[str, Any], subject_vendors: frozenset[str]) -> RubricScore:
        user = json.dumps({"rubric": criteria, "material": material}, ensure_ascii=False, indent=2)
        request = mg.GenerateRequest(
            tenant=tenant_pb2.TenantContext(tenant_id=self.tenant_id),
            call=mg.CallContext(
                node_id="rubric_judge",
                role=enums_pb2.ROLE_JUDGE,
                tier=self.tier,
                # 被测对象用了几家时只能填一家；返回后对全部供应商再核对一次。
                avoid_vendor=min(subject_vendors) if subject_vendors else "",
            ),
            generation=mg.GenerateParams(
                messages=[mg.ChatMessage(role=mg.CHAT_ROLE_SYSTEM, text=_SYSTEM), mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=user)],
                output_schema=payload_pb2.JsonPayload(value=_value(_OUTPUT_SCHEMA)),
            ),
        )
        response = await self.gateway.generate(request)
        vendor = response.model.vendor
        if vendor in subject_vendors:
            raise SameVendorError(f"judge 由 {vendor} 服务，与被测对象同一家")
        result = json_format.MessageToDict(response.result)
        score = result.get("score") if isinstance(result, dict) else None
        if not isinstance(score, int | float) or isinstance(score, bool) or not 0 <= score <= 1:
            raise ValueError(f"judge 给出的分数不在 0 到 1 之间：{score!r}")
        return RubricScore(score=float(score), vendor=vendor, cost_usd=response.usage.cost_usd_micros / 1_000_000)
