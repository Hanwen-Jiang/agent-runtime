"""S-01/D-23/D-24 acceptance inventory.

This is a review gate, not an implementation test: the runtime contract does not
exist until S-01 approves the fields. Keeping the cases as data prevents a later
schema/API change from silently dropping an ownership, secret, lifecycle, network,
or compatibility requirement.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "tests" / "contract" / "fixtures" / "environment_contract_cases.json"
BASELINE = ROOT / "docs" / "plan" / "s01-environment-contract-baseline.md"


def _data() -> dict:
    return json.loads(CASES.read_text(encoding="utf-8"))


def test_environment_acceptance_inventory_is_unique_and_complete() -> None:
    data = _data()
    cases = data["cases"]
    ids = [case["id"] for case in cases]
    assert data["schema"] == "environment_contract_cases"
    assert data["version"] == 1
    assert len(ids) == len(set(ids))
    assert {case["area"] for case in cases} == {"ownership", "concurrency", "secrets", "lifecycle", "network", "binding", "compatibility"}
    assert {case["expect"] for case in cases} == {"allow", "deny", "conflict"}
    assert all(case["assertion"].strip() for case in cases)


def test_inventory_is_grounded_in_the_candidate_baseline() -> None:
    baseline = BASELINE.read_text(encoding="utf-8")
    required_terms = (
        "PRIVATE",
        "expected_version",
        "secret_refs",
        "setup",
        "start_on_failure",
        "DENY",
        "ALLOWLIST",
        "OPEN",
        "D-25",
        "D-26",
        "D-23 第二阶段",
    )
    missing = [term for term in required_terms if term not in baseline]
    assert not missing, f"acceptance cases no longer match the candidate baseline: {missing}"
