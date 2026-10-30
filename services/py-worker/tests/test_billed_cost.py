"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 11.11 节问题 2、3：已计费的失败与美元预算用完时不再重问。

- 网关的错误在 details 里带 billed_cost_usd_micros（这次 RPC 里已计费的花费）；Python 客户端把它还原到错误上，
  `executor.model_gateway.billed_cost_usd(exc)` 取出（美元），`with_billed_cost(exc, micros)` 构造。
- Planner、Reviewer 的调用失败时，把已计费的花费记进这次的 calls（冒烟里两次失败的评审各 0.33、0.52 美元没记上）。
- 回答不合格时两者都重问一次；美元预算已经用完时不再重问（11.4 定的“评审前不拦”不变：第一次规划与评审照常）：
  - Planner：Task 预算减去之前各 Round 的成本，再减去这次已调的（分诊与第一次规划），不大于 0 就直接用保守规划；
  - Reviewer：台账的已花（含本轮的规划与 Stage）加上这次第一次评审的花费，不小于预算就按规则判定。
  没有美元预算时照旧重问。
"""

from __future__ import annotations

from typing import Any

import grpc
import pytest

from agentruntime.common.v1 import error_pb2
from agentruntime_core.errors import InvalidError, TransientError

from agentruntime_worker.executor.model_gateway import billed_cost_usd, with_billed_cost
from agentruntime_worker.planner.planner import Planner
from agentruntime_worker.reviewer.reviewer import Reviewer

from . import reviewer_support as rs
from .gateway_fakes import Abort, FakeModelGateway, generate_request, open_client, provider_error, serve
from .planner_support import FakeGateway, plan_answer, request
from .test_planner_full import RISKY_FULL
from .test_reviewer import REPORT_OK, REPORT_STAGE

BILLED_COST_DETAIL = "billed_cost_usd_micros"


def unexpected_tool_call(micros: int) -> InvalidError:
    """网关报的“回答里有没给的工具调用”，已计费 micros。"""
    return with_billed_cost(InvalidError("unexpected_tool_call", "模型调用了请求里没给的工具"), micros)


# --- Python 客户端还原已计费的花费 ---


async def test_the_client_restores_the_billed_cost_from_the_error_details() -> None:
    error = provider_error(error_pb2.ERROR_CATEGORY_INVALID, "unexpected_tool_call", "模型调用了请求里没给的工具", {BILLED_COST_DETAIL: 435})
    fake = FakeModelGateway(abort=Abort(grpc.StatusCode.INVALID_ARGUMENT, "不合格", error))
    async with serve(fake) as address, open_client(address) as gateway:
        with pytest.raises(InvalidError) as info:
            await gateway.generate(generate_request())
    assert info.value.code == "unexpected_tool_call"
    assert billed_cost_usd(info.value) == pytest.approx(0.000435)


async def test_an_error_without_a_billed_cost_reports_zero() -> None:
    error = provider_error(error_pb2.ERROR_CATEGORY_TRANSIENT, "models_exhausted", "同档的模型都暂时不可用")
    fake = FakeModelGateway(abort=Abort(grpc.StatusCode.UNAVAILABLE, "不可用", error))
    async with serve(fake) as address, open_client(address) as gateway:
        with pytest.raises(TransientError) as info:
            await gateway.generate(generate_request())
    assert billed_cost_usd(info.value) == 0.0


def test_billed_cost_of_other_exceptions_is_zero() -> None:
    assert billed_cost_usd(RuntimeError("别的异常")) == 0.0
    assert billed_cost_usd(unexpected_tool_call(400_000)) == pytest.approx(0.4)


# --- Planner、Reviewer 记上已计费的失败 ---


def spent(calls: list[Any]) -> float:
    return sum(c.cost_usd for c in calls)


async def test_the_planner_counts_a_billed_failure() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=[unexpected_tool_call(400_000), plan_answer()])
    result = await Planner(gateway).plan(request())
    assert not result.fallback and len(gateway.calls("plan")) == 2
    plan_costs = sorted(c.cost_usd for c in result.calls if c.purpose == "plan")
    assert plan_costs == [pytest.approx(0.02), pytest.approx(0.4)], "失败的那次也记进 calls"
    assert spent(result.calls) == pytest.approx(0.0002 + 0.4 + 0.02)


async def test_the_reviewer_counts_a_billed_failure() -> None:
    gateway = rs.FakeReviewGateway([unexpected_tool_call(300_000), rs.done("report")])
    state = rs.task_state(rounds=rs.rounds("continue"), done_when="简报成稿且引用核对通过")
    result = await Reviewer(gateway).review(rs.request(REPORT_STAGE, REPORT_OK, state=state))
    assert result.verdict["verdict"] == "done" and len(gateway.requests) == 2
    assert spent(result.calls) == pytest.approx(0.3 + 0.03), "失败的那次也记进 calls"


# --- 美元预算用完时不再重问 ---


def planner_state(*, budget: float, earlier: float) -> dict[str, Any]:
    """第 2 轮规划时的 Task State：第 1 轮花了 earlier 美元。"""
    first = {"seq": 1, "stage_goal": "取数", "status": "completed", "verdict": "continue", "cost_usd": earlier}
    return rs.task_state(rounds=[first], budget=budget)


async def test_the_planner_does_not_ask_again_once_the_budget_is_spent() -> None:
    # 1.7 + 分诊 0.0002 + 第一次规划 0.4 > 2：不重问，直接用保守规划。
    gateway = FakeGateway(triage=RISKY_FULL, plan=["不是 JSON", plan_answer()], cost_micros={"triage": 200, "plan": 400_000})
    result = await Planner(gateway).plan(request(round_seq=2, task_state=planner_state(budget=2.0, earlier=1.7)))
    assert len(gateway.calls("plan")) == 1, "美元预算用完了就不再重问"
    assert result.fallback and result.problems
    [node] = result.stage["nodes"]
    assert node["kind"] == "goal" and node["mode"] == "tool_loop"
    assert spent(result.calls) == pytest.approx(sum(200 if r.call.node_id.startswith("triage") else 400_000 for r in gateway.requests) / 1e6)


async def test_the_planner_still_asks_again_while_budget_remains() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=["不是 JSON", plan_answer()], cost_micros={"triage": 200, "plan": 400_000})
    result = await Planner(gateway).plan(request(round_seq=2, task_state=planner_state(budget=2.0, earlier=0.5)))
    assert len(gateway.calls("plan")) == 2 and not result.fallback


async def test_the_planner_without_a_budget_asks_again() -> None:
    state = planner_state(budget=2.0, earlier=5.0)
    del state["constraints"]["budget_usd"]
    gateway = FakeGateway(triage=RISKY_FULL, plan=["不是 JSON", plan_answer()], cost_micros={"triage": 200, "plan": 400_000})
    result = await Planner(gateway).plan(request(round_seq=2, task_state=state))
    assert len(gateway.calls("plan")) == 2 and not result.fallback, "没有美元预算就不限"


def reviewer_state(*, budget: float | None, earlier: float, current: float) -> dict[str, Any]:
    return rs.task_state(rounds=rs.rounds("continue", costs=[earlier], current_cost=current), budget=budget, done_when="简报成稿且引用核对通过")


async def test_the_reviewer_does_not_ask_again_once_the_budget_is_spent() -> None:
    # 已花 1.0 + 本轮 0.95，第一次评审 0.1 之后到 2.05 ≥ 2：不重问，按规则判定。
    gateway = rs.FakeReviewGateway(["我觉得已经完成了", rs.done("report")], cost_micros=100_000)
    result = await Reviewer(gateway).review(rs.request(REPORT_STAGE, REPORT_OK, state=reviewer_state(budget=2.0, earlier=1.0, current=0.95)))
    assert len(gateway.requests) == 1, "美元预算用完了就不再重问"
    assert result.fallback and result.problems
    assert spent(result.calls) == pytest.approx(0.1)


async def test_the_reviewer_still_asks_again_while_budget_remains() -> None:
    gateway = rs.FakeReviewGateway(["我觉得已经完成了", rs.done("report")], cost_micros=100_000)
    result = await Reviewer(gateway).review(rs.request(REPORT_STAGE, REPORT_OK, state=reviewer_state(budget=2.0, earlier=0.2, current=0.3)))
    assert len(gateway.requests) == 2 and not result.fallback and result.verdict["verdict"] == "done"


async def test_the_first_review_is_not_skipped_even_when_the_budget_is_spent() -> None:
    """11.4 人定：评审前不拦（评审可能判 done，拦了反而交付不了）。"""
    gateway = rs.FakeReviewGateway([rs.done("report")], cost_micros=100_000)
    result = await Reviewer(gateway).review(rs.request(REPORT_STAGE, REPORT_OK, state=reviewer_state(budget=2.0, earlier=1.5, current=0.8)))
    assert len(gateway.requests) == 1 and not result.fallback, "第一次评审照常调用，模型的回答照常用（之后的去向由 floor 定）"
