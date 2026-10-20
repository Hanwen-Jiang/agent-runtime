"""S-08 验收“人审评测集”的可自动检查部分：仓库里的评测集（evalsets/task-loop）合格、覆盖够、划分合理。

人审本身是人工的：2026-10-20 经两轮人审，64 条标注全部确认（review.status = confirmed）。
以后改了标注或新增用例，先回到草稿（review.status = draft），test_every_label_is_confirmed 会失败，直到人再次确认。
"""

from __future__ import annotations

from collections import Counter
from functools import cache

import pytest

from agentruntime_learning.evals.cases import CATEGORIES, DEFAULT_EVAL_SET, SYMPTOMS, EvalSet, load_eval_set

from support import REPO_ROOT

MAIN_VERDICTS = {"done", "continue", "repair", "replan", "escalate", "abort"}
ALL_VERDICTS = MAIN_VERDICTS | {"rollback", "reroute"}
TRIAGE_LEVELS = {"fast", "light", "full", "replan"}
EFFECT_ORDER = ["none", "read", "write_reversible", "write_irreversible"]


@cache
def eval_set() -> EvalSet:
    return load_eval_set(DEFAULT_EVAL_SET)


def test_the_eval_set_loads_without_problems() -> None:
    es = eval_set()
    assert es.name == "task-loop"


def test_there_are_30_to_50_task_cases() -> None:
    assert 30 <= len(eval_set().task_cases) <= 50


def test_task_cases_cover_the_six_task_categories() -> None:
    counts = Counter(c.category for c in eval_set().task_cases)
    assert set(CATEGORIES) <= set(counts), f"缺少：{set(CATEGORIES) - set(counts)}"
    assert all(counts[c] >= 3 for c in CATEGORIES), counts


def test_task_cases_cover_the_four_triage_levels_in_both_splits() -> None:
    for split in ("dev", "holdout"):
        levels = Counter(c.planner_expect["triage"] for c in eval_set().task_cases if c.split == split)
        assert set(levels) == TRIAGE_LEVELS, f"{split} 缺少：{TRIAGE_LEVELS - set(levels)}"


def test_task_cases_cover_the_main_verdicts() -> None:
    verdicts = {cp.expect["verdict"] for c in eval_set().task_cases for cp in c.rounds}
    assert MAIN_VERDICTS <= verdicts, f"缺少：{MAIN_VERDICTS - verdicts}"


def test_some_task_cases_run_several_rounds() -> None:
    """期望的 Verdict 序列：至少有几条多轮用例（附录 B.3 的三个 Round）。"""
    assert sum(1 for c in eval_set().task_cases if len(c.rounds) >= 3) >= 3


def test_the_rule_checks_are_exercised() -> None:
    cases = eval_set().task_cases
    rising = [c for c in cases if EFFECT_ORDER.index(c.planner_expect["risk"]) > EFFECT_ORDER.index(c.planner_input["context"]["risk_before"])]
    assert len(rising) >= 3 and all(c.planner_expect["triage"] == "full" for c in rising), "风险上升一律走 full"
    assert sum(1 for c in cases if c.planner_expect.get("irreversible_actions")) >= 3, "write_irreversible 前必须有 gate"
    assert all(c.planner_input["context"].get("recipes") for c in cases if c.planner_expect["triage"] == "light")
    replans = [c for c in cases if c.planner_expect["triage"] == "replan"]
    assert all(c.planner_input.get("task_state", {}).get("rounds") for c in replans), "replan 要带着之前的 Round"
    assert any(c.planner_expect["shape"].get("keep_nodes") for c in replans), "replan 保留已验证的 Node"
    assert any("forbidden_roles" in c.planner_expect["shape"] for c in cases), "一轮只规划到下一个信息检查点"


def test_reviewer_samples_cover_every_verdict_and_every_b7_symptom() -> None:
    samples = eval_set().reviewer_samples
    assert len(samples) >= 20
    verdicts = {s.checkpoint.expect["verdict"] for s in samples}
    assert verdicts == ALL_VERDICTS, f"缺少：{ALL_VERDICTS - verdicts}"
    symptoms = {s.symptom for s in samples if s.symptom}
    assert symptoms == set(SYMPTOMS), f"缺少：{set(SYMPTOMS) - symptoms}"
    stuck = [s for s in samples if s.checkpoint.expect["min_escalation"] not in ("none", "repair_retry")]
    assert len(stuck) >= 5, "要有足够的“该升级却原地重试”的考点"


@pytest.mark.parametrize("kind", ["task_cases", "reviewer_samples"])
def test_dev_and_holdout_are_split(kind: str) -> None:
    items = getattr(eval_set(), kind)
    holdout = sum(1 for i in items if i.split == "holdout")
    assert 0.25 <= holdout / len(items) <= 0.4, f"留出集占 {holdout}/{len(items)}"


def test_every_case_carries_a_review_status() -> None:
    es = eval_set()
    assert all(i.review.get("status") in ("draft", "confirmed") for i in [*es.task_cases, *es.reviewer_samples])


def test_every_label_is_confirmed() -> None:
    assert eval_set().unconfirmed() == []


def test_holdout_cases_do_not_leak_into_the_worker() -> None:
    """提示只在开发集上调：留出集的消息与 id 不得出现在 Worker 的代码与提示里。"""
    es = eval_set()
    needles = [c.planner_input["message"] for c in es.task_cases if c.split == "holdout"]
    needles += [i.id for i in [*es.task_cases, *es.reviewer_samples] if i.split == "holdout"]
    sources = [p for p in (REPO_ROOT / "services" / "py-worker" / "src").rglob("*") if p.is_file() and p.suffix in {".py", ".md", ".txt", ".json", ".j2", ".jinja", ".yaml", ".yml"}]
    leaks = [(p.name, n) for p in sources for n in needles if n in p.read_text("utf-8", errors="ignore")]
    assert not leaks, leaks
