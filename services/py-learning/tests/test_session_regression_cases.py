"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 11.13 节：Session 上下文的回归样本。

样本在单独的 evalsets/task-loop-session-drafts：标注还没经人确认（task-loop 要求全部确认），内容是编的，不含租户的内容。
跑：python -m agentruntime_learning.evals run --eval-set evalsets/task-loop-session-drafts --subject <…>。人确认后再移进 task-loop。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentruntime_worker.session.transcript import Turn

from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET, load_eval_set
from agentruntime_learning.evals.model import PlannerInput
from agentruntime_learning.evals.runner import UnconfirmedLabelsError, run_eval
from agentruntime_learning.evals.subjects import load_subject
from agentruntime_learning.evals.worker_subject import _context

DRAFTS = Path(DEFAULT_EVAL_SET).parent / "task-loop-session-drafts"


def drafts():
    return load_eval_set(DRAFTS)


def test_the_draft_set_loads_and_every_label_is_unconfirmed() -> None:
    es = drafts()
    assert len(es.task_cases) == 4
    assert es.unconfirmed() == sorted(c.id for c in es.task_cases), "标注要经人确认"


def test_the_drafts_stay_out_of_the_main_set() -> None:
    main = {c.id for c in load_eval_set(DEFAULT_EVAL_SET).task_cases}
    assert not main & {c.id for c in drafts().task_cases}


def test_the_catalog_is_the_same_as_the_main_set() -> None:
    assert (DRAFTS / "catalog.json").read_bytes() == (Path(DEFAULT_EVAL_SET) / "catalog.json").read_bytes()


def test_the_cases_cover_both_sides_of_the_routing() -> None:
    """要读工作区的追问不走 fast；只凭对话能答的、与仓库无关的问题也不要多花钱走完整规划。"""
    levels = {c.id: c.planner_expect["triage"] for c in drafts().task_cases}
    assert sorted(levels.values()) == ["fast", "fast", "full", "full"]
    assert all(c.planner_input["context"]["has_workspace"] is True and c.planner_input["context"]["conversation"] for c in drafts().task_cases)


def test_the_harness_passes_the_conversation_to_the_planner() -> None:
    es = drafts()
    case = es.task_cases[0]
    ctx = _context(PlannerInput(message=case.planner_input["message"], task_state=None, attachments=[], context=case.planner_input["context"], catalog=es.catalog))
    assert ctx.has_workspace is True
    assert all(isinstance(t, Turn) for t in ctx.conversation) and ctx.conversation[0].reply


def test_cases_without_a_conversation_keep_the_old_context() -> None:
    es = load_eval_set(DEFAULT_EVAL_SET)
    case = es.task_cases[0]
    ctx = _context(PlannerInput(message=case.planner_input["message"], task_state=None, attachments=[], context=case.planner_input["context"], catalog=es.catalog))
    assert ctx.conversation == () and ctx.has_workspace is False


def test_the_reference_answers_score_full_marks() -> None:
    es = drafts()
    report = asyncio.run(run_eval(es, load_subject("oracle", es), reports_dir=None))
    planner = report.data["metrics"]["planner"]
    assert planner["triage_accuracy"] == pytest.approx(1.0) and planner["shape_match_rate"] == pytest.approx(1.0)


def test_requiring_confirmed_labels_rejects_the_drafts() -> None:
    es = drafts()
    with pytest.raises(UnconfirmedLabelsError):
        asyncio.run(run_eval(es, load_subject("oracle", es), reports_dir=None, require_confirmed=True))
