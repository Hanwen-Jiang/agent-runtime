"""公开数据集 CellEval 的测试用：按脚本作答的假 Model Gateway，与把手写的小样本写成真实格式的工具。"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import metering_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_learning.capability.base import role_name

DEFAULT_PROFILE = "a6api/default@1"

Answer = str | Mapping[str, Any] | mg.GenerateResponse | BaseException


class ScriptGateway:
    """answer(request) 返回文字、{"text": …, "tool_calls": [(名字, 参数), …], "result": …}、完整的响应或要抛的错误。
    实际服务的模型取钉的 Profile（没钉时 DEFAULT_PROFILE），每次调用记 cost_micros 的花费。"""

    def __init__(self, answer: Callable[[mg.GenerateRequest], Answer], *, cost_micros: int = 1000) -> None:
        self.answer = answer
        self.cost_micros = cost_micros
        self.requests: list[mg.GenerateRequest] = []

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        self.requests.append(request)
        out = self.answer(request)
        if isinstance(out, BaseException):
            raise out
        if isinstance(out, mg.GenerateResponse):
            return out
        response = mg.GenerateResponse(
            finish_reason=mg.FINISH_REASON_STOP,
            model=mg.ModelChoice(model_profile_id=request.call.pinned_model_profile or DEFAULT_PROFILE),
            usage=metering_pb2.Usage(input_tokens=100, output_tokens=10, cost_usd_micros=self.cost_micros),
        )
        if isinstance(out, str):
            response.text = out
            return response
        response.text = str(out.get("text") or "")
        for n, (name, args) in enumerate(out.get("tool_calls") or ()):
            call = response.tool_calls.add(id=f"call_{len(self.requests)}_{n}", name=name)
            call.arguments.update(dict(args))
        if "result" in out:
            value = struct_pb2.Value()
            json_format.ParseDict(out["result"], value)
            response.result.CopyFrom(value)
        return response

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        raise AssertionError("这些评测不做嵌入")

    def roles(self) -> list[str]:
        return [role_name(r.call.role) for r in self.requests]


def text_of(request: mg.GenerateRequest) -> str:
    """请求里全部消息的文字，按顺序接起来。"""
    return "\n".join(m.text for m in request.generation.messages)


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    return path
