"""Regression coverage for the invalid plan-score publication discovered in S09-DRIFT."""

from __future__ import annotations

import copy

import pytest

from agentruntime_learning.capability import publication as pub, scoring
from agentruntime_learning.capability.publish import PublishError, patch_from_scores
from agentruntime_learning.capability.evals.s09 import S09Plan, scoring_contract
from agentruntime_worker.planner import prompts
from test_capability_scoring import binary, rows


def assess(data, candidates=("a",), *, repeats=3, role="plan"):
    cell = {"role": role, **scoring.score_cell(data, candidates).to_json()}
    pub.enforce(cell, data, expected_repeats={"e": repeats})
    return cell


def test_perfect_small_samples_and_missing_repeats_cannot_publish():
    small = assess(rows("a", binary(12, 1, r=3)))
    assert small["candidates"][0]["half_width"] == 0
    assert not small["candidates"][0]["publishable"]
    assert "minimum_40_items" in small["publication"]["blocked"]["a"]
    incomplete = assess(rows("a", binary(40, 1, r=2)))
    assert "incomplete_repeats" in incomplete["publication"]["blocked"]["a"]
    assert assess(rows("a", binary(40, 1, r=3)))["candidates"][0]["publishable"]


def test_duplicate_records_do_not_inflate_evidence():
    data = rows("a", binary(40, 1, r=3))
    cell = assess([*data, data[0]])
    assert not cell["candidates"][0]["publishable"]
    assert "duplicate_observations" in cell["publication"]["blocked"]["a"]


def test_different_questions_cannot_be_compared_as_the_same_cell():
    a = rows("a", binary(40, 1, r=3))
    b = rows("b", binary(40, 1, r=3))
    for row in b:
        if row["item"] == "e-0":
            row["item"] = "replacement-easy-question"
    cell = assess(a + b, ("a", "b"))
    assert all(not c["publishable"] for c in cell["candidates"])
    assert all("different_item_sets" in reasons for reasons in cell["publication"]["blocked"].values())


def test_minor_candidate_coverage_blocks_cell_without_erasing_scores():
    data = rows("a", binary(40, 0.9, r=3))
    cell = assess(data, ("a", "b", "c"))
    assert cell["candidates"][0]["score"] is not None
    assert all(not c["publishable"] for c in cell["candidates"])
    assert cell["publication"]["eligible_candidates"] == 1
    assert cell["publication"]["required_candidates"] == 2
    assert cell["problems"]


def test_old_planning_records_stay_unpublishable_when_rescored():
    data = rows("a", binary(40, 1, r=3), eval_name="s09")
    cell = {"role": "plan", **scoring.score_cell(data, ["a"]).to_json()}
    pub.enforce(cell, data, expected_repeats={"s09": 3})
    assert "legacy_planning_evidence" in cell["publication"]["blocked"]["a"]
    assert not cell["candidates"][0]["publishable"]
    fresh = copy.deepcopy(data)
    for r in fresh:
        r["record"] = {"scoring_contract": scoring_contract()}
        r["experiment_sha256"] = "a" * 64
        r["deployment_frozen"] = True
    cell = {"role": "plan", **scoring.score_cell(fresh, ["a"]).to_json()}
    pub.enforce(cell, fresh, expected_repeats={"s09": 3})
    assert cell["candidates"][0]["publishable"]
    assert cell["publication"]["evidence_sha256"] != pub.evidence_digest(data)
    for row in fresh:
        row["record"]["scoring_contract"]["implementation_sha256"] = "not-even-a-digest"
    cell = {"role": "plan", **scoring.score_cell(fresh, ["a"]).to_json()}
    pub.enforce(cell, fresh, expected_repeats={"s09": 3})
    assert not cell["candidates"][0]["publishable"]


def test_legacy_publishable_flag_does_not_bypass_publication_policy():
    with pytest.raises(PublishError, match="重新 score"):
        patch_from_scores({"cells": [{"publishable": True}]})


def test_planner_version_tracks_actual_prompt_and_validation_sources():
    contract = scoring_contract()
    assert contract["id"] == pub.S09_CONTRACT
    assert len(contract["implementation_sha256"]) == 64
    ev = S09Plan()
    assert ev.version != ev.eval_set.version[:12]
    assert ev.contract == contract
    assert ev.items(47, 0) == S09Plan().items(47, 0)
    # These are generic design rules, not case-specific answers or weaker labels.
    assert all(term in prompts._PLANNING_PATTERNS for term in ("template", "revise", "human", "deliver", "Hook", "单一写者"))
