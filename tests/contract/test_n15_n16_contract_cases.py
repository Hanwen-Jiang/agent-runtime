"""Candidate acceptance inventory for N-15 user MCP and N-16 multi-agent work."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "contract" / "fixtures" / "n15_n16_contract_cases.json"
BASELINE = ROOT / "docs" / "plan" / "n15-n16-contract-baseline-2026-11-02.md"


def _data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_n15_n16_inventory_is_unique_and_covers_boundaries() -> None:
    data = _data()
    cases = data["cases"]
    ids = [case["id"] for case in cases]
    assert data["schema"] == "n15_n16_contract_cases"
    assert data["version"] == 1
    assert len(ids) == len(set(ids))
    assert {case["feature"] for case in cases} == {"N-15", "N-16"}
    assert {case["area"] for case in cases} >= {
        "ownership", "authorization", "secrets", "privacy", "isolation", "lifecycle",
        "compatibility", "identity", "concurrency", "cancellation", "quota", "observability",
    }
    assert {case["expect"] for case in cases} == {"allow", "deny", "conflict"}
    assert all(case["assertion"].strip() for case in cases)


def test_inventory_matches_design_and_cost_decisions() -> None:
    baseline = BASELINE.read_text(encoding="utf-8")
    required_terms = (
        "MCP Gateway", "SecretStore", "secret_ref", "effect", "嵌套深度不超过 2",
        "共享产物同一时刻只有一个写者", "Failure", "Minimum fix", "Performance cost",
        "blocked_external", "DEFER IMPLEMENTATION",
    )
    missing = [term for term in required_terms if term not in baseline]
    assert not missing, f"N-15/N-16 inventory no longer matches baseline: {missing}"


def test_each_feature_has_positive_and_negative_cases() -> None:
    by_feature = {feature: [] for feature in ("N-15", "N-16")}
    for case in _data()["cases"]:
        by_feature[case["feature"]].append(case["expect"])
    for feature, outcomes in by_feature.items():
        assert "allow" in outcomes, feature
        assert "deny" in outcomes, feature
