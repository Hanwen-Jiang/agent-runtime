"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 4 节验收：规划与评审调用卡住时看得见、有总时限。

细化见该计划第 11 节（与第 4 节原文的不同：Python 的 activity 推不了进度事件——推送是 Go 的 Session Worker 上的
publish_task_event，只能由工作流排——所以重试从服务端挪进工作流，放在版本分支后面）：
- 新的运行里 plan_round、review_round、tool_loop_step 每次只尝试一次（服务端不重试），由工作流按同样的退避重试，
  最多 DECISION_MAX_ATTEMPTS 次；第 2 次起每次尝试之前推 decision_retry（哪一步、第几次、上次的错误代码与说明）。
  不可重试的错误（non_retryable）不重试、不推。
- 一步决策的总时限 DECISION_TOTAL（从第一次尝试算）：尝试用完或到了总时限，规划改用不调模型的保守规划
  （PlanRoundInput.no_model），评审改用只按规则判定（ReviewRoundInput.rules_only），Task 不悬着；tool_loop 的一步照旧让节点失败。
- 错误代码取 activity 失败的类型（ApplicationError.type，例如 models_exhausted、grpc_deadline_exceeded），超时记 activity_timeout。
- Model Gateway 的 models_exhausted 在 details 里带上最后一次失败的代码（last_error_code），Worker 把它当作错误代码
  （Go 一侧的测试在 services/go-model-gateway/internal/gateway/tmp_fixes_test.go）。
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from agentruntime.common.v1 import enums_pb2

from agentruntime_worker.activities.types import ToolLoopStep
from agentruntime_worker.planner.activities import PlannerActivities
from agentruntime_worker.planner.context import StaticContextSource
from agentruntime_worker.planner.planner import Planner
from agentruntime_worker.workflows import patches, timeouts

from .planner_support import FakeGateway, context
from .reviewer_support import FakeReviewGateway
from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .support import assert_valid
from .test_planner_activity import plan_input
from .test_reviewer_activity import activities as reviewer_activities
from .test_reviewer_activity import review_input
from .test_stage_runner import deliver, goal, one_round, stage
from .test_task_verdicts import out, scripted

def retries(h: StageHarness, task_id: str) -> list[dict[str, Any]]:
    return [e for e in h.script.progress(task_id) if e["event"] == "decision_retry"]


def flaky(failures: int, error_type: str, then: Any) -> Any:
    """前 failures 次抛 error_type 的可重试错误，之后按 then 作答（函数）。"""
    seen = {"n": 0}

    def answer(inp: Any) -> Any:
        seen["n"] += 1
        if seen["n"] <= failures:
            raise ApplicationError(f"第 {seen['n']} 次：上游不可用", type=error_type)
        return then(inp)

    return answer


# --- 工作流：重试看得见 ---


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_retried_plan_round_publishes_decision_retry_before_each_new_attempt(harness: StageHarness) -> None:
    s = harness.script
    one_round(harness, deliver())
    first = s.plans["main"][0]
    s.plan_for = flaky(2, "models_exhausted", lambda inp: first)
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    got = retries(harness, inp.task_id)
    assert [(e["step"], e["attempt"], e["max_attempts"], e.get("error_code")) for e in got] == [
        ("plan_round", 2, timeouts.DECISION_MAX_ATTEMPTS, "models_exhausted"),
        ("plan_round", 3, timeouts.DECISION_MAX_ATTEMPTS, "models_exhausted"),
    ]
    assert all("上游不可用" in e["error"] for e in got)
    kinds = [e["event"] for e in harness.script.progress(inp.task_id)]
    assert kinds.index("decision_retry") < kinds.index("stage_started"), "重试的事件在这一轮的 Stage 定下来之前"
    for e in got:
        assert_valid("task_event_payload", e)


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_retried_review_round_publishes_decision_retry(harness: StageHarness) -> None:
    s = harness.script
    one_round(harness, deliver())
    done = out("done", "完成", done={"deliver_node": "answer"})

    async def review(inp: Any) -> Any:
        return done

    calls = {"n": 0}

    async def flaky_review(inp: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ApplicationError("网关超时", type="grpc_deadline_exceeded")
        return await review(inp)

    s.reviewer = flaky_review
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    got = retries(harness, inp.task_id)
    assert [(e["step"], e["round_seq"], e["attempt"], e.get("error_code")) for e in got] == [("review_round", 1, 2, "grpc_deadline_exceeded")]


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_retried_tool_loop_step_names_its_goal_node(harness: StageHarness) -> None:
    s = harness.script
    s.tool_steps["explore"] = flaky(1, "models_exhausted", lambda inp: ToolLoopStep(action="finish", result={"ok": True}))
    one_round(harness, goal("explore", "tool_loop", tools=["repo_read"], max_iterations=3), deliver(after=["explore"]))
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    got = retries(harness, inp.task_id)
    assert [(e["step"], e.get("node_id"), e["attempt"]) for e in got] == [("tool_loop_step", "explore", 2)]


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_non_retryable_failures_are_not_retried_or_announced(harness: StageHarness) -> None:
    s = harness.script

    def broken(inp: Any) -> Any:
        raise ApplicationError("输入不合法", type="invalid_plan_input", non_retryable=True)

    s.plan_for = broken
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_FAILED
    assert len(s.plan_inputs) == 1 and not retries(harness, inp.task_id)


# --- 工作流：重试用完不悬着 ---


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_planning_falls_back_to_a_plan_without_the_model_when_attempts_run_out(harness: StageHarness) -> None:
    s = harness.script
    fallback = stage(1, goal("explore", "tool_loop", tools=["repo_read"], max_iterations=1), deliver(after=["explore"]))
    s.tool_steps["explore"] = lambda inp: ToolLoopStep(action="finish", result={"ok": True})

    def plan(inp: Any) -> Any:
        if inp.no_model:
            return fallback
        raise ApplicationError("同档的模型都暂时不可用", type="models_exhausted")

    s.plan_for = plan
    s.deliver["main"] = "answer"
    s.plans["main"] = [fallback]
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    attempts = [p for p in s.plan_inputs if p.task_id == inp.task_id]
    assert len(attempts) == timeouts.DECISION_MAX_ATTEMPTS + 1
    assert [p.no_model for p in attempts] == [False] * timeouts.DECISION_MAX_ATTEMPTS + [True]
    assert [e["attempt"] for e in retries(harness, inp.task_id)] == list(range(2, timeouts.DECISION_MAX_ATTEMPTS + 1))


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_review_falls_back_to_the_rules_when_attempts_run_out(harness: StageHarness) -> None:
    s = harness.script
    one_round(harness, deliver())
    seen: list[bool] = []

    async def review(inp: Any) -> Any:
        seen.append(inp.rules_only)
        if inp.rules_only:
            return out("done", "各 Node 都成功，deliver 节点给出了结果", done={"deliver_node": "answer"})
        raise ApplicationError("评审模型不可用", type="models_exhausted")

    s.reviewer = review
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert seen == [False] * timeouts.DECISION_MAX_ATTEMPTS + [True]


def test_the_total_limit_leaves_room_for_the_fallback() -> None:
    """总时限比单次尝试长（至少能试两次），又比原来最坏的约 27 分钟短得多。"""
    assert timeouts.DECISION_TOTAL >= 2 * timeouts.MODEL_DECISION_START_TO_CLOSE
    assert timeouts.DECISION_TOTAL.total_seconds() <= 10 * 60
    assert timeouts.DECISION_MAX_ATTEMPTS == timeouts.DECISION_RETRY.maximum_attempts


# --- activity：不调模型的保守规划与只按规则的评审 ---


class NoModel:
    """调到就失败的网关：no_model、rules_only 时不应调模型。"""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def generate(self, request: Any) -> Any:
        self.requests.append(request)
        raise AssertionError("不应调用模型")


async def test_plan_round_without_the_model_returns_the_conservative_plan() -> None:
    gateway = NoModel()
    activities = PlannerActivities(Planner(gateway), StaticContextSource(context()))  # type: ignore[arg-type]
    out_ = await ActivityEnvironment().run(activities.plan_round, plan_input(no_model=True))
    assert not gateway.requests
    assert_valid("stage", out_.stage)
    goals = [n for n in out_.stage["nodes"] if n.get("kind") == "goal"]
    assert goals and goals[0].get("mode") == "tool_loop", "保守规划：一个 tool_loop 的探索节点"
    assert out_.task_plan["triage"]["task_family"] == "general.unclassified"


async def test_review_round_with_rules_only_does_not_call_the_model() -> None:
    gateway = FakeReviewGateway([])
    out_ = await ActivityEnvironment().run(reviewer_activities(gateway).review_round, dataclasses.replace(review_input(), rules_only=True))
    assert not gateway.requests
    assert_valid("verdict", out_.verdict)
    assert out_.verdict["reason_code"] == "rule_fallback", "按规则判定的理由带代码（第 5 节）"


# --- 版本分支 ---


def test_the_new_version_branches_are_appended_at_the_end_of_at_start() -> None:
    """第 3 节与第 4 节各一个版本分支，按这个顺序紧接着 PROGRESS_DETAIL_PATCH 追加（之前的不动）；之后只能再往末尾追加
    （第 5 节的 CODES_PATCH，再之后的由各自的测试看）。"""
    i = patches.AT_START.index(patches.PROGRESS_DETAIL_PATCH)
    assert patches.AT_START[i + 1 : i + 4] == (patches.BUDGETS_PATCH, patches.DECISION_RETRY_PATCH, patches.CODES_PATCH)

