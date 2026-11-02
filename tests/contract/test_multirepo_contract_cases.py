"""D-25 multi-repository workspace acceptance inventory.

The runtime contract is not implemented until S-01 approves the fields and RPCs.
These cases keep the required compatibility, path, authorization, credential,
lifecycle, and replay behavior reviewable without changing generated contracts.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "tests" / "contract" / "fixtures" / "multirepo_contract_cases.json"
BASELINE = ROOT / "docs" / "plan" / "d25-multirepo-contract-baseline.md"


def _data() -> dict:
    return json.loads(CASES.read_text(encoding="utf-8"))


def test_multirepo_inventory_is_unique_and_complete() -> None:
    data = _data()
    cases = data["cases"]
    ids = [case["id"] for case in cases]
    assert data["schema"] == "multirepo_contract_cases"
    assert data["version"] == 1
    assert len(ids) == len(set(ids))
    assert {case["area"] for case in cases} == {
        "compatibility", "layout", "path", "quota", "authorization", "credentials",
        "network", "lifecycle", "replay", "ownership",
    }
    assert {case["expect"] for case in cases} == {"allow", "deny"}
    assert all(case["assertion"].strip() for case in cases)


def test_inventory_is_grounded_in_d25_baseline() -> None:
    baseline = BASELINE.read_text(encoding="utf-8")
    required_terms = (
        "root", "multi", "AttachRepo", "DetachRepo", "ListRepos", "repo_read_only",
        "sealed_credential", "max_repos", "max_bytes", "golden", "D-25",
    )
    missing = [term for term in required_terms if term not in baseline]
    assert not missing, f"D-25 cases no longer match the candidate baseline: {missing}"
