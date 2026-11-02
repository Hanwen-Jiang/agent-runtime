"""能力矩阵测试的假模型与合成的题（不花钱、确定）。

- ServingGateway：像 Model Gateway 那样选档——钉了就服务钉的 Profile（fail_pins 里的除外），没钉按 Tier 给默认的；
  回答交给 answer(request) 或 inner（例如 S-09 的 ScriptedGateway、S-11 的 ExtractiveGateway）；记下每个请求。
- SyntheticEval（make）：pool 道题，每道题调一次被测角色；候选 c 在第 i 道题上答对 ⇔ i % 100 < skill[c] * 100，
  所以原始分精确等于 skill（题数是 100 的倍数时）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from agentruntime.common.v1 import metering_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

from agentruntime_learning.capability.base import Item, ItemResult, role_enum

REPO = Path(__file__).resolve().parents[3]
DEFAULTS = {
    "small@low": "a6api/gpt-4o-mini@2026-10",
    "mid@low": "a6api/deepseek-v4-flash@2026-10",
    "strong@medium": "a6api/claude-sonnet-5-5@2026-10",
    "strong@high": "a6api/claude-sonnet-5-5@2026-10",
}
SONNET = "a6api/claude-sonnet-5-5@2026-10"
OPUS = "a6api/claude-opus-5-5@2026-10"
GPT55 = "a6api/gpt-5.5@2026-10"
SOL = "a6api/gpt-6.1-sol@2026-11"
MINI = "a6api/gpt-4o-mini@2026-10"
FLASH = "a6api/deepseek-v4-flash@2026-10"


class ServingGateway:
    def __init__(
        self,
        inner: Any = None,
        *,
        answer: Callable[[mg.GenerateRequest], Any] | None = None,
        fail_pins: tuple[str, ...] = (),
        switch_pins: Mapping[str, str] | None = None,
        cost_micros: int = 1000,
    ) -> None:
        self.inner = inner
        self.answer = answer
        self.fail_pins = fail_pins
        self.switch_pins = dict(switch_pins or {})
        self.cost_micros = cost_micros
        self.requests: list[mg.GenerateRequest] = []

    def served(self, request: mg.GenerateRequest) -> str:
        pin = request.call.pinned_model_profile
        if pin:
            return self.switch_pins.get(pin, pin)
        return DEFAULTS.get(request.call.tier, "a6api/default@1")

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        self.requests.append(request)
        if request.call.pinned_model_profile in self.fail_pins:
            raise RuntimeError("provider down")
        if self.inner is not None:
            response = await self.inner.generate(request)
        else:
            response = mg.GenerateResponse(finish_reason=mg.FINISH_REASON_STOP)
            answer = self.answer(request) if self.answer else "ok"
            if isinstance(answer, str):
                response.text = answer
            else:
                from google.protobuf import json_format, struct_pb2

                value = struct_pb2.Value()
                json_format.ParseDict(answer, value)
                response.result.CopyFrom(value)
        out = mg.GenerateResponse()
        out.CopyFrom(response)
        out.model.model_profile_id = self.served(request)
        out.model.tier = request.call.tier
        if not out.usage.cost_usd_micros:
            out.usage.CopyFrom(metering_pb2.Usage(cost_usd_micros=self.cost_micros))
        return out

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        raise AssertionError("不做嵌入")


class SyntheticEval:
    """合成的一套题；skill：候选 → 答对的比例。"""

    version = "v1"

    def __init__(self, *, role: str = "plan", pool: int = 200, skill: Mapping[str, float] | None = None, name: str = "synthetic", uses_judge: bool = False, est_tokens: tuple[int, int] = (1000, 100)) -> None:
        self.role = role
        self.name = name
        self.pool = pool
        self.skill = dict(skill or {})
        self.uses_judge = uses_judge
        self.est_tokens = est_tokens
        self.est_other_usd = 0.0
        self.calls: list[tuple[str, str, int]] = []

    def items(self, n: int, seed: int) -> list[Item]:
        ids = [f"q{i:03d}" for i in range(self.pool)]
        rotated = ids[seed % self.pool :] + ids[: seed % self.pool]
        return [Item(id=i, payload={"index": int(i[1:])}) for i in rotated[:n]]

    async def run_item(self, item: Item, gateway: Any, *, candidate: str) -> ItemResult:
        request = mg.GenerateRequest()
        request.call.role = role_enum(self.role)
        request.call.tier = "strong@medium"
        request.call.node_id = f"synthetic#{item.id}"
        response = await gateway.generate(request)
        if self.uses_judge:
            judge = mg.GenerateRequest()
            judge.call.role = role_enum("judge")
            judge.call.tier = "strong@medium"
            judge.generation.messages.append(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=f"判 {item.id}"))
            await gateway.generate(judge)
        self.calls.append((candidate, item.id, len(self.calls)))
        index = int(item.payload["index"])
        right = index % 100 < round(self.skill.get(candidate, 0.5) * 100)
        return ItemResult(item.id, score=1.0 if right else 0.0, record={"served": response.model.model_profile_id, "index": index})


_EVALS: dict[str, SyntheticEval] = {}


def make(key: str = "default", **options: Any) -> SyntheticEval:
    """计划里写 capability_support:make 加 options；同一个 key 返回同一个实例（测试要看它被调了几次）。"""
    if key not in _EVALS:
        _EVALS[key] = SyntheticEval(**options)
    return _EVALS[key]


def reset() -> None:
    _EVALS.clear()


def write_plan(path: Path, **plan: Any) -> Path:
    path.write_text(json.dumps(plan, ensure_ascii=False), "utf-8")
    return path


class FakeTelemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, float, dict[str, Any]]] = []

    async def count(self, name: str, value: int = 1, *, attributes: dict[str, Any] | None = None) -> None:
        self.events.append(("count", name, value, dict(attributes or {})))

    async def observe(self, name: str, value: float, *, attributes: dict[str, Any] | None = None) -> None:
        self.events.append(("observe", name, value, dict(attributes or {})))

    async def flush(self) -> None:
        pass
