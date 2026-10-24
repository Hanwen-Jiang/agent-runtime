"""T-16 第 4 步：观测式评测与非劣效门槛（附录 J.3；细化见 docs/plan/learning-pipeline.md 第 2 节第 4 步、第 4 节第 1、2 条）。"""

from __future__ import annotations

import pytest

from agentruntime_learning.evals.schemas import problems, validator
from agentruntime_learning.pipeline.candidates import mine_recipe
from agentruntime_learning.pipeline.evaluation import (
    Outcomes,
    baseline_cohort,
    candidate_cohort,
    evidence,
    non_inferiority,
    wilson,
)
from agentruntime_learning.pipeline.traces import TaskTrace, read_spans, task_traces

from pipeline_support import FAMILY, OTHER_FAMILY, ROUTE_A, ROUTE_B, TEMPLATE_EFFECTS, TaskSpec, console_lines, population, route_task


def traces(specs: list[TaskSpec]) -> list[TaskTrace]:
    return task_traces(read_spans(console_lines(specs, noise=False), fmt="console"), tenant_id="t1").tasks


def gate_for(specs: list[TaskSpec], *, baseline_recipe: str | None = None, **kw: object):  # noqa: ANN201
    tasks = traces(specs)
    candidate = mine_recipe(tasks, FAMILY, templates=TEMPLATE_EFFECTS, min_support=5)
    assert candidate is not None
    cand = Outcomes.from_tasks(candidate_cohort(tasks, FAMILY, candidate.signature))
    base = Outcomes.from_tasks(baseline_cohort(tasks, FAMILY, baseline_recipe))
    return non_inferiority(cand, base, **kw), cand, base


# --- 统计量 ---


def test_wilson_interval() -> None:
    low, high = wilson(90, 100)
    assert low == pytest.approx(0.8256, abs=1e-4) and high == pytest.approx(0.9448, abs=1e-4)
    assert wilson(0, 10)[0] == 0.0 and wilson(10, 10)[1] == pytest.approx(1.0)


def test_wilson_needs_samples() -> None:
    with pytest.raises(ValueError):
        wilson(0, 0)


def test_cost_per_success_counts_the_failures_too() -> None:
    """R-06 评审 1.1：每次成功成本 = 组内全部 Task 的成本 ÷ 成功数（H.2、S-08 报告的 per_success_usd），失败的花费也算。"""
    tasks = traces([route_task(1, ROUTE_A, cost=0.2), route_task(2, ROUTE_A, status="aborted", cost=0.5), route_task(3, ROUTE_B, cost=0.4)])
    o = Outcomes.from_tasks(tasks)
    assert (o.total, o.successes) == (3, 2)
    assert o.total_cost == pytest.approx(1.1)
    assert o.success_rate == pytest.approx(2 / 3)
    assert o.cost_per_success == pytest.approx(0.55)


def test_the_cost_upper_bound_is_the_ratio_estimators() -> None:
    """95% 上界 = R + 1.96 × SE，R = Σc ÷ Σs，SE = sd(c − R·s) ÷ (√n · 成功率)（比率估计量的 delta 方法，确定、不抽样）。"""
    tasks = traces([route_task(1, ROUTE_A, cost=0.2), route_task(2, ROUTE_A, status="aborted", cost=0.5), route_task(3, ROUTE_B, cost=0.4)])
    o = Outcomes.from_tasks(tasks)
    # 残差 −0.35、0.5、−0.15：样本标准差 √(0.395 / 2)；成功率 2/3。
    se = (0.395 / 2) ** 0.5 / (3**0.5 * (2 / 3))
    assert o.cost_upper_bound() == pytest.approx(0.55 + 1.96 * se)
    assert Outcomes.from_tasks(traces([route_task(1, ROUTE_A, status="aborted")])).cost_upper_bound() is None, "没有成功就没有每次成功成本"


def test_expensive_failures_make_a_route_costlier() -> None:
    """成功的都便宜、失败的很贵：只看成功的均值会通过成本门槛，算上失败的花费就不通过。"""
    specs = [
        *[route_task(i, ROUTE_A, cost=0.05) for i in range(118)],
        *[route_task(200 + i, ROUTE_A, status="aborted", cost=5.0) for i in range(2)],
        *[route_task(300 + i, ROUTE_B, cost=0.1) for i in range(100)],
        *[route_task(400 + i, ROUTE_B, status="aborted", cost=0.1) for i in range(10)],
    ]
    gate, cand, base = gate_for(specs, min_samples=100)
    assert cand.cost_per_success == pytest.approx((118 * 0.05 + 2 * 5.0) / 118)
    assert not gate.passed
    assert any("成本" in r for r in gate.reasons)
    assert not any("成功率" in r for r in gate.reasons), "成功率这一条是通过的，只报成本"


# --- 两组 ---


def test_candidate_cohort_includes_failures_and_partial_failures() -> None:
    specs = population(a_success=8, a_fail=3, a_partial_fail=4, b_success=5)
    tasks = traces(specs)
    candidate = mine_recipe(tasks, FAMILY, templates=TEMPLATE_EFFECTS, min_support=5)
    assert candidate is not None
    cohort = candidate_cohort(tasks, FAMILY, candidate.signature)
    assert len(cohort) == 15, "成功 8 + 走完失败 3 + 走到一半失败 4；B 路线不算"
    assert {t.status for t in cohort} == {"done", "aborted"}


def test_baseline_without_a_recipe_is_all_full_planning_of_the_family() -> None:
    specs = [*population(a_success=6, a_fail=2, b_success=4, b_fail=1), *population(a_success=9, a_fail=0, family=OTHER_FAMILY, salt="o"),
             *[route_task(200 + i, ROUTE_A, recipe="invoice_std@v2") for i in range(3)]]
    assert len(baseline_cohort(traces(specs), FAMILY, None)) == 13


def test_baseline_with_a_default_recipe_is_the_tasks_that_used_it() -> None:
    specs = [*population(a_success=6, a_fail=2, b_success=4), *[route_task(200 + i, ROUTE_B, recipe="invoice_std@v2", status="done" if i % 2 else "aborted") for i in range(6)],
             *[route_task(300 + i, ROUTE_B, recipe="invoice_std@v1") for i in range(4)]]
    cohort = baseline_cohort(traces(specs), FAMILY, "invoice_std@v2")
    assert len(cohort) == 6 and all(t.recipe == "invoice_std@v2" for t in cohort), "只取现行默认版本的 Task"


# --- 门槛 ---


def test_a_better_route_passes() -> None:
    gate, cand, base = gate_for(population(a_success=115, a_fail=5, b_success=85, b_fail=35), min_samples=100)
    assert (cand.total, base.total) == (120, 240)
    assert gate.passed, gate.reasons


def test_a_lower_success_rate_fails() -> None:
    gate, _, _ = gate_for(population(a_success=100, a_fail=40, b_success=90, b_fail=2), min_samples=100)
    assert not gate.passed
    assert any("成功率" in r for r in gate.reasons)


def test_a_costlier_route_fails() -> None:
    gate, _, _ = gate_for(population(a_success=118, a_fail=2, b_success=110, b_fail=10, a_cost=0.2, b_cost=0.05), min_samples=100)
    assert not gate.passed
    assert any("成本" in r for r in gate.reasons)
    assert not any("成功率" in r for r in gate.reasons), "成功率这一条是通过的，只报成本"


def test_too_few_samples_never_pass() -> None:
    gate, cand, _ = gate_for(population(a_success=50, a_fail=0, b_success=10, b_fail=40), min_samples=100)
    assert cand.total == 50
    assert not gate.passed
    assert any("样本" in r for r in gate.reasons)


def test_counting_only_successes_would_be_selection_bias() -> None:
    """走 A 路线的 Task 一半到中途就失败了：只看成功的会是 100%，正确的候选组是 50%，不能通过。"""
    gate, cand, _ = gate_for(population(a_success=110, a_fail=0, a_partial_fail=110, b_success=100, b_fail=20), min_samples=100)
    assert cand.total == 220 and cand.successes == 110
    assert not gate.passed


def test_thresholds_are_parameters() -> None:
    specs = population(a_success=110, a_fail=20, b_success=100, b_fail=4)
    strict, _, _ = gate_for(specs, min_samples=100)
    lenient, _, _ = gate_for(specs, min_samples=100, delta=0.2, epsilon=0.5)
    assert not strict.passed and lenient.passed
    assert lenient.thresholds == {"delta": 0.2, "epsilon": 0.5}


def test_defaults_follow_appendix_j3() -> None:
    gate, _, _ = gate_for(population(a_success=115, a_fail=5, b_success=85, b_fail=35))
    assert gate.thresholds == {"delta": 0.02, "epsilon": 0.05}
    assert gate.min_samples == 100


# --- 证据 ---


def test_evidence_matches_the_registry_schema() -> None:
    gate, _, _ = gate_for(population(a_success=115, a_fail=5, b_success=85, b_fail=35))
    doc = evidence(gate, task_family=FAMILY, task_ids=["tsk_b", "tsk_a"], report="finance.invoice_coding-20261023T090000Z.json", evaluated_at="2026-10-23T09:00:00Z")
    assert problems(validator("registry_evaluation"), doc) == []
    assert doc["eval_set"]["name"] == "traces.finance.invoice_coding"
    assert doc["passed"] is True and doc["samples"] == 120
    assert doc["candidate"]["success_rate"] == pytest.approx(115 / 120)
    low, high = doc["candidate"]["success_rate_ci95"]
    assert low < 115 / 120 < high
    assert doc["thresholds"] == {"delta": 0.02, "epsilon": 0.05}
    assert "waiver" not in doc, "Learning 不自己豁免"


def test_evidence_version_identifies_the_trace_set() -> None:
    gate, _, _ = gate_for(population(a_success=115, a_fail=5, b_success=85, b_fail=35))
    a = evidence(gate, task_family=FAMILY, task_ids=["tsk_1", "tsk_2"], report="r", evaluated_at="2026-10-23T09:00:00Z")
    b = evidence(gate, task_family=FAMILY, task_ids=["tsk_2", "tsk_1"], report="r", evaluated_at="2026-10-23T09:00:00Z")
    c = evidence(gate, task_family=FAMILY, task_ids=["tsk_1", "tsk_3"], report="r", evaluated_at="2026-10-23T09:00:00Z")
    assert a["eval_set"]["version"] == b["eval_set"]["version"] != c["eval_set"]["version"]


def test_failed_evidence_still_validates() -> None:
    gate, _, _ = gate_for(population(a_success=100, a_fail=40, b_success=90, b_fail=2))
    doc = evidence(gate, task_family=FAMILY, task_ids=["tsk_1"], report="r.json", evaluated_at="2026-10-23T09:00:00Z")
    assert doc["passed"] is False
    assert problems(validator("registry_evaluation"), doc) == []
