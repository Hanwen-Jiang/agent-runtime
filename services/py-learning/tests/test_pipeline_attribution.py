"""T-16：失败归因（附录 J.2）与单独的预算（3.5.1 关键规则；细化见 docs/plan/learning-pipeline.md 第 2 节“另外两件”）。

归因用小模型经 Model Gateway，输入只有结局、各 Round 的 verdict 与 Stage 形状；预算只算 Learning 自己的模型调用，用完就不再调用。
"""

from __future__ import annotations

import json

import pytest

from agentruntime.common.v1 import enums_pb2
from agentruntime_learning.pipeline.attribution import CATEGORIES, LEARNING_USER, attribute_failures
from agentruntime_learning.pipeline.budget import LearningBudget
from agentruntime_learning.pipeline.traces import TaskTrace, read_spans, task_traces

from pipeline_support import FAMILY, ROUTE_A, ROUTE_B, ScriptedGateway, console_lines, route_task


def failed_tasks(n: int = 3) -> list[TaskTrace]:
    specs = [route_task(i, ROUTE_A if i % 2 else ROUTE_B, status="aborted") for i in range(n)]
    specs.append(route_task(99, ROUTE_A, status="done"))
    return task_traces(read_spans(console_lines(specs, noise=False), fmt="console"), tenant_id="t1").tasks


def test_categories_follow_mast() -> None:
    assert CATEGORIES == ("specification", "coordination", "verification")


async def test_each_failed_task_gets_the_models_category() -> None:
    gateway = ScriptedGateway([{"category": "verification", "reason": "没有 judge"}, {"category": "coordination", "reason": "Node 之间不一致"},
                               {"category": "specification", "reason": "验收条件不清"}])
    tasks = failed_tasks()
    results = await attribute_failures(tasks, tenant_id="t1", task_family=FAMILY, gateway=gateway, budget=LearningBudget(limit_usd=1))
    assert len(results) == 3, "成功的 Task 不归因"
    assert sorted(r.category for r in results) == ["coordination", "specification", "verification"]
    assert {r.task_id for r in results} == {t.task_id for t in tasks if t.status != "done"}


async def test_calls_use_the_small_tier_with_the_tenant_and_no_task() -> None:
    gateway = ScriptedGateway([])
    await attribute_failures(failed_tasks(1), tenant_id="t1", task_family=FAMILY, gateway=gateway, budget=LearningBudget(limit_usd=1))
    [request] = gateway.requests
    assert request.tenant.tenant_id == "t1" and request.tenant.user_id == LEARNING_USER
    assert request.call.tier == "small@low"
    assert request.call.task_id == "" and request.call.node_id == "", "Learning 的调用不计入任何 Task"
    assert request.call.task_family == FAMILY
    assert request.call.role == enums_pb2.ROLE_JUDGE
    assert request.generation.HasField("output_schema"), "结构化输出，按 schema 校验"


async def test_the_prompt_only_carries_outcome_verdicts_and_shapes() -> None:
    gateway = ScriptedGateway([])
    [task] = [t for t in failed_tasks(1) if t.status != "done"]
    await attribute_failures([task], tenant_id="t1", task_family=FAMILY, gateway=gateway, budget=LearningBudget(limit_usd=1))
    text = "\n".join(m.text for m in gateway.requests[0].generation.messages)
    assert task.status in text
    for r in task.rounds:
        assert r.verdict in text
        assert json.dumps(r.shape, ensure_ascii=False, sort_keys=True) in text
    assert task.task_id not in text and "t1" not in text, "不需要的标识不进提示"


@pytest.mark.parametrize("bad", [{"category": "bad_luck", "reason": "x"}, {"reason": "缺类别"}, "不是对象"])
async def test_invalid_model_output_is_unattributed(bad: object) -> None:
    results = await attribute_failures(failed_tasks(1), tenant_id="t1", task_family=FAMILY, gateway=ScriptedGateway([bad]), budget=LearningBudget(limit_usd=1))
    assert [r.category for r in results] == ["unattributed"]


async def test_without_a_gateway_everything_is_unattributed() -> None:
    budget = LearningBudget(limit_usd=1)
    results = await attribute_failures(failed_tasks(), tenant_id="t1", task_family=FAMILY, gateway=None, budget=budget)
    assert {r.category for r in results} == {"unattributed"}
    assert budget.calls == 0 and budget.spent_usd == 0


async def test_gateway_errors_are_unattributed_not_fatal() -> None:
    class Down(ScriptedGateway):
        async def generate(self, request):  # noqa: ANN001, ANN201
            self.requests.append(request)
            raise ConnectionError("Gateway 不可用")

    results = await attribute_failures(failed_tasks(2), tenant_id="t1", task_family=FAMILY, gateway=Down([]), budget=LearningBudget(limit_usd=1))
    assert {r.category for r in results} == {"unattributed"}


# --- 预算 ---


async def test_spending_is_charged_from_the_gateways_usage() -> None:
    budget = LearningBudget(limit_usd=1)
    await attribute_failures(failed_tasks(3), tenant_id="t1", task_family=FAMILY, gateway=ScriptedGateway([], cost_micros=2500), budget=budget)
    assert budget.calls == 3
    assert budget.spent_usd == pytest.approx(0.0075)


async def test_each_call_is_capped_by_the_remaining_budget() -> None:
    gateway = ScriptedGateway([], cost_micros=3000)
    await attribute_failures(failed_tasks(3), tenant_id="t1", task_family=FAMILY, gateway=gateway, budget=LearningBudget(limit_usd=0.01))
    caps = [r.call.max_cost_usd_micros for r in gateway.requests]
    assert caps == [10000, 7000, 4000]


async def test_an_exhausted_budget_stops_model_calls() -> None:
    gateway = ScriptedGateway([], cost_micros=6000)
    budget = LearningBudget(limit_usd=0.01)
    results = await attribute_failures(failed_tasks(3), tenant_id="t1", task_family=FAMILY, gateway=gateway, budget=budget)
    assert len(gateway.requests) == 2, "第二次之后剩余预算为负，不再调用"
    assert budget.exhausted
    assert [r.category for r in results].count("unattributed") == 1


def test_budget_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        LearningBudget(limit_usd=-1)
    assert LearningBudget(limit_usd=0).exhausted, "预算为 0 表示不调用模型"


def test_budget_report() -> None:
    budget = LearningBudget(limit_usd=0.5)
    budget.charge(120000)
    assert budget.report() == {"limit_usd": 0.5, "spent_usd": 0.12, "calls": 1, "exhausted": False}
