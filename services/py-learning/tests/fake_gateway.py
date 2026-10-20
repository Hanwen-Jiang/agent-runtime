"""S-09 评测用的假 Model Gateway：分类器与规划都按脚本作答，记下每个请求。

make() 是命令行的工厂（AR_EVAL_GATEWAY_FACTORY=fake_gateway:make）：一个“什么都答不对”的模型，
用来证明结构校验与规则检查由代码兜底，不靠模型。
"""

from __future__ import annotations

from typing import Any

from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import metering_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg


class ScriptedGateway:
    """triage、plan 是按请求返回结构化结果（dict）或文本（str）的函数；None 表示回答一段不合格的文本。"""

    def __init__(self, *, triage: Any = None, plan: Any = None, vendor: str = "vendor_a", plan_cost_micros: int = 20_000) -> None:
        self.triage = triage
        self.plan = plan
        self.vendor = vendor
        self.plan_cost_micros = plan_cost_micros
        self.requests: list[mg.GenerateRequest] = []

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        self.requests.append(request)
        is_triage = request.call.node_id.startswith("triage")
        handler = self.triage if is_triage else self.plan
        answer = handler(request) if handler else "我觉得可以这样做……"
        response = mg.GenerateResponse(
            finish_reason=mg.FINISH_REASON_STOP,
            model=mg.ModelChoice(model_profile_id=f"{self.vendor}/{request.call.tier}", tier=request.call.tier, vendor=self.vendor),
            usage=metering_pb2.Usage(cost_usd_micros=200 if is_triage else self.plan_cost_micros),
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


def make() -> ScriptedGateway:
    return ScriptedGateway()
