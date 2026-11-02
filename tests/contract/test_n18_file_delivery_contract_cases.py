"""S-01 preflight inventory for N-18 file delivery."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "contract" / "fixtures" / "n18_file_delivery_contract_cases.json"
BASELINE = ROOT / "docs" / "plan" / "n18-file-delivery-contract-baseline-2026-11-03.md"


def _data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_n18_inventory_is_unique_and_covers_delivery_boundaries() -> None:
    data = _data()
    cases = data["cases"]
    ids = [case["id"] for case in cases]
    assert data["schema"] == "n18_file_delivery_contract_cases"
    assert data["version"] == 1
    assert len(ids) == len(set(ids))
    assert {case["area"] for case in cases} >= {
        "metadata", "claim_check", "limits", "paths", "content_type", "isolation",
        "authorization", "expiry", "privacy", "idempotency", "partial_failure",
        "compatibility", "streaming",
    }
    assert {case["expect"] for case in cases} == {"allow", "deny", "conflict"}
    assert all(case["assertion"].strip() for case in cases)


def test_inventory_matches_design_and_cost_decisions() -> None:
    baseline = BASELINE.read_text(encoding="utf-8")
    required_terms = (
        "TaskWorkflowResult", "ObjectStore / Claim-Check", "tenant/session/task",
        "短期下载引用", "幂等键", "Failure", "Minimum fix", "Performance cost",
        "blocked_external", "第二套持久状态机", "DEFER",
    )
    missing = [term for term in required_terms if term not in baseline]
    assert not missing, f"N-18 inventory no longer matches baseline: {missing}"


def test_inventory_has_positive_negative_and_partial_outcomes() -> None:
    outcomes = {case["expect"] for case in _data()["cases"]}
    assert {"allow", "deny", "conflict"} <= outcomes
