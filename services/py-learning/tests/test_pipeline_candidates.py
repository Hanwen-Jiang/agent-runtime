"""T-16 第 3 步：从 trace 归纳候选 Recipe（附录 J.4；细化见 docs/plan/learning-pipeline.md 第 2 节第 3 步）。"""

from __future__ import annotations

import pytest

from agentruntime_learning.evals.schemas import problems, validator
from agentruntime_learning.pipeline.candidates import mine_recipe, recipe_name, signature
from agentruntime_learning.pipeline.traces import TaskTrace, read_spans, task_traces

from pipeline_support import (
    CHECK,
    FAMILY,
    OTHER_FAMILY,
    ROUTE_A,
    ROUTE_B,
    SHAPE_A1,
    SHAPE_A2,
    TEMPLATE_EFFECTS,
    TaskSpec,
    console_lines,
    population,
    route_task,
)


def traces(specs: list[TaskSpec], tenant: str = "t1") -> list[TaskTrace]:
    return task_traces(read_spans(console_lines(specs), fmt="console"), tenant_id=tenant).tasks


def mine(specs: list[TaskSpec], **kw: object):  # noqa: ANN201
    kw.setdefault("templates", TEMPLATE_EFFECTS)
    kw.setdefault("min_support", 5)
    return mine_recipe(traces(specs), FAMILY, **kw)


def test_signature_is_structure_only() -> None:
    [a] = traces([route_task(1, ROUTE_A)])
    reworded = ({**SHAPE_A1, "goal": "换个说法取数"}, {**SHAPE_A2, "exit_when": "换个说法"})
    [b] = traces([route_task(2, reworded)])
    assert signature(a) == signature(b), "goal、exit_when 的措辞不影响签名"
    retiered = ({**SHAPE_A1, "nodes": [SHAPE_A1["nodes"][0], {**SHAPE_A1["nodes"][1], "tier": "strong@medium"}]}, SHAPE_A2)
    [c] = traces([route_task(3, retiered)])
    assert signature(a) != signature(c), "Tier 是做法的一部分"


def test_rerun_rounds_are_not_part_of_the_signature() -> None:
    [plain] = traces([route_task(1, ROUTE_A)])
    [rerun] = traces([route_task(2, ROUTE_A, rerun=True)])
    assert signature(plain) == signature(rerun)


def test_the_most_successful_route_becomes_the_candidate() -> None:
    candidate = mine(population(a_success=8, a_fail=1, b_success=5, b_fail=0))
    assert candidate is not None
    assert candidate.support == 8
    stages = candidate.document["stages"]
    assert [s["goal"] for s in stages] == [SHAPE_A1["goal"], SHAPE_A2["goal"]]
    assert [[n["id"] for n in s["nodes"]] for s in stages] == [["fetch", "code"], ["post", "check"]]


def test_below_min_support_there_is_no_candidate() -> None:
    assert mine(population(a_success=4, a_fail=0, b_success=3)) is None


def test_ties_break_deterministically() -> None:
    specs = population(a_success=6, a_fail=0, b_success=6)
    first = mine(specs)
    second = mine(list(reversed(specs)))
    assert first is not None and second is not None
    assert first.signature == second.signature and first.document == second.document


def test_only_the_family_and_only_full_planning_count() -> None:
    specs = [
        *population(a_success=3, a_fail=0),
        *population(a_success=10, a_fail=0, family=OTHER_FAMILY, salt="other"),
        *[route_task(100 + i, ROUTE_A, recipe="invoice_std@v2") for i in range(10)],
    ]
    assert mine(specs) is None, "别的任务族与已经用 Recipe 的 Task 不算支持"


def test_the_document_is_a_valid_recipe() -> None:
    candidate = mine(population(a_success=6, a_fail=2))
    assert candidate is not None
    assert problems(validator("recipe"), candidate.document) == []
    doc = candidate.document
    assert doc["task_family"] == FAMILY
    assert doc["description"]
    goal_node = doc["stages"][1]["nodes"][1]
    assert goal_node == CHECK, "Goal Node 原样保留 goal、done_when、mode、output_schema、after"
    for stage in doc["stages"]:
        for node in stage["nodes"]:
            assert not {"executor", "effect", "role", "output", "idempotency_key", "inputs", "rationale"} & set(node)


def test_risk_follows_the_templates_and_goal_nodes_count_as_irreversible() -> None:
    with_goal = mine(population(a_success=6, a_fail=0))
    assert with_goal is not None and with_goal.document["risk"] == "write_irreversible", "有 Goal Node 时按 write_irreversible（第 4 节第 3 条）"
    atomic_only = (SHAPE_A1, {**SHAPE_A2, "nodes": [SHAPE_A2["nodes"][0]]})
    specs = [route_task(i, atomic_only) for i in range(6)]
    candidate = mine(specs)
    assert candidate is not None and candidate.document["risk"] == "write_reversible", "atomic 节点模板 effect 的最大值"


def test_unknown_templates_mean_no_candidate() -> None:
    assert mine(population(a_success=6, a_fail=0), templates={"invoice_fetch@v1": "read"}) is None, "查不到模板的 effect 就算不出 risk"


def test_typical_cost_is_the_median_of_successes() -> None:
    specs = [route_task(i, ROUTE_A, cost=c) for i, c in enumerate([0.1, 0.2, 0.3, 0.4, 5.0])]
    specs.append(route_task(99, ROUTE_A, status="aborted", cost=9.0))
    candidate = mine(specs)
    assert candidate is not None
    assert candidate.document["typical_cost_usd"] == pytest.approx(0.3)


def test_name_is_scoped_to_the_family() -> None:
    assert recipe_name("finance.invoice_coding") == "learned_finance_invoice_coding"
    candidate = mine(population(a_success=6, a_fail=0))
    assert candidate is not None and candidate.name == "learned_finance_invoice_coding"
