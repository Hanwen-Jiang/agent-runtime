"""能力矩阵的共同接口（capability.base）：钉模型、污染判定与结果的取值范围。"""

from __future__ import annotations

from typing import Any

import pytest

from agentruntime.common.v1 import enums_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

from agentruntime_learning.capability.base import Cell, ItemResult, PinningGateway, role_enum, role_name

CANDIDATE = "a6api/claude-opus-5-5@2026-10"


class Fake:
    def __init__(self, *, serve: str | None = None, fail: bool = False) -> None:
        self.serve, self.fail = serve, fail
        self.seen: list[mg.GenerateRequest] = []

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        self.seen.append(request)
        if self.fail:
            raise RuntimeError("provider down")
        profile = self.serve or request.call.pinned_model_profile or "a6api/default@1"
        out = mg.GenerateResponse(text="ok")
        out.model.model_profile_id = profile
        out.usage.cost_usd_micros = 2500
        return out

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        raise NotImplementedError


def request(role: str) -> mg.GenerateRequest:
    r = mg.GenerateRequest()
    r.call.role = role_enum(role)
    return r


async def test_only_the_role_under_test_is_pinned() -> None:
    inner = Fake()
    gw = PinningGateway(inner, role="plan", candidate=CANDIDATE)
    original = request("plan")
    await gw.generate(original)
    await gw.generate(request("route"))
    assert inner.seen[0].call.pinned_model_profile == CANDIDATE and inner.seen[1].call.pinned_model_profile == ""
    assert original.call.pinned_model_profile == "", "不改调用方的请求"
    assert not gw.contaminated and gw.cost_usd == pytest.approx(0.005)
    assert gw.calls[("plan", CANDIDATE)] == 1


@pytest.mark.parametrize("fake", [Fake(serve="a6api/other@1"), Fake(fail=True)], ids=["switched", "failed"])
async def test_a_pinned_call_that_misses_the_candidate_contaminates(fake: Any) -> None:
    gw = PinningGateway(fake, role="judge", candidate=CANDIDATE)
    try:
        await gw.generate(request("judge"))
    except RuntimeError:
        pass
    assert gw.contaminated


async def test_other_roles_falling_back_do_not_contaminate() -> None:
    gw = PinningGateway(Fake(serve="a6api/other@1"), role="plan", candidate=CANDIDATE)
    await gw.generate(request("route"))
    assert not gw.contaminated


def test_roles_and_scores_are_checked() -> None:
    assert role_name(role_enum("deliver")) == "deliver" and role_name(enums_pb2.ROLE_UNSPECIFIED) == "unspecified"
    with pytest.raises(ValueError):
        Cell("general.unclassified", "planner", "medium")
    with pytest.raises(ValueError):
        ItemResult("x", score=1.5)
