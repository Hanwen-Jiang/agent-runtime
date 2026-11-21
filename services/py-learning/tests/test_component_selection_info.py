"""Run-level cohort provenance distinguishes cohorts, executions and missing records."""
from __future__ import annotations

import json

from agentruntime_learning.evals.public_capability import project_scores
from agentruntime_learning.evals.public_cohorts import PUBLIC_LONGBENCH_40, digest, longbench_selection
from agentruntime_learning.evals.public_experiments import _memory_cohort, _paired_cohort
from agentruntime_learning.evals.public_task_loop import project_report


def test_matrix_planned_limits_are_not_model_counts_and_mixed_suites_keep_different_rules():
    col = {"role": "judge", "tier": "medium"}
    plan = {"seed": 7, "n": 40, "cells": [{**col, "evals": ["s10", {"eval": "rewardbench2", "n": 30}]}]}
    scores = {"cells": [{**col, "evals": [{"name": "s10", "version": "v1"}, {"name": "rewardbench2", "version": "v2"}],
                         "candidates": [{"candidate": "model", "observed_cohorts":
                              {"s10": {"count": 14, "sha256": "b" * 64, "item_ids": ["private-ID"]}}}]}]}
    run = project_scores("mixed", scores, plan, "d" * 64)
    groups = run["selection_info"]["groups"]
    assert [(g["method"], g["requested_count"], g["seed"]) for g in groups] == [
        ("seeded_shuffle_prefix", 40, 7), ("seeded_round_robin", 30, 7)]
    info = run["models"][0]["cells"]["general.unclassified/judge.medium"]["selection_info"]
    assert info["groups"][0]["observed_count"] == 14 and info["groups"][0]["cohort_sha256"] == "b" * 64
    assert "selected_count" not in groups[0] and "private-ID" not in json.dumps(run)


def test_task_loop_holdout_never_exposes_ids_or_claims_legacy_exhausts_pool():
    base = {"subject": "worker", "split": "holdout", "created_at": "2026-11-12T00:00:00Z", "metrics": {},
            "eval_set": {"name": "task-loop", "version": "fed321"}, "labels": {"confirmed": 3, "draft": 1},
            "cases": [{"id": "secret-case"}]}
    old = project_report(base, "d" * 64)["selection_info"]
    assert old["selected_count"] == 4 and old["method"] == "historical_unrecorded" and "population_count" not in old
    report = {**base, "selection": {"method": "predefined_split", "selected_count": 4, "population_count": 11,
                                    "selection_ids": ["secret-case"], "cohort_sha256": "e" * 64}}
    new = project_report(report, "d" * 64)["selection_info"]
    assert new["population_count"] == 11 and new["method"] == "predefined_split"
    assert "secret-case" not in json.dumps(new)


def test_historical_run_cannot_borrow_sampling_from_another_same_size_run():
    assert "seed" not in longbench_selection({"sample_count": 40, "cohort_sha256": "b" * 64})
    assert longbench_selection({"sample_count": 40, "cohort_sha256": PUBLIC_LONGBENCH_40})["seed"] == 0
    manifest = {"question_ids": ["q1", "q2"], "provenance": {"seed": 3, "limit": 2}}
    assert "seed" not in _memory_cohort("c" * 64, {}, 2, manifest)
    verified = _memory_cohort("c" * 64, {"manifest_digest": digest(manifest)}, 2, manifest)
    assert verified["seed"] == 3 and "question_ids" not in verified
    pairs = _paired_cohort([{"case_id": "secret-q"}], {"original_case_ids": ["secret-q"]})
    assert pairs["selected_count"] == 1 and pairs["method"] == "frozen_cohort" and "secret-q" not in json.dumps(pairs)
