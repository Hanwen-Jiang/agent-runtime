"""N-04 contract inventory before schema/event implementation."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "tests" / "contract" / "fixtures" / "task_plan_contract_cases.json"
BASELINE = ROOT / "docs" / "plan" / "n04-task-plan-contract-baseline.md"


def test_task_plan_inventory_is_unique_and_covers_boundaries() -> None:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert data["schema"] == "task_plan_contract_cases"
    assert data["version"] == 1
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["area"] for case in cases} == {"steps", "status", "round", "event", "context", "compatibility"}
    assert {case["expect"] for case in cases} == {"allow", "deny"}
    assert all(case["assertion"].strip() for case in cases)


def test_inventory_matches_candidate_baseline() -> None:
    baseline = BASELINE.read_text(encoding="utf-8")
    required = ("3～8", "plan_updated", "pending", "active", "blocked", "fast", "旧客户端", "Task State")
    missing = [term for term in required if term not in baseline]
    assert not missing, f"N-04 inventory drifted from candidate baseline: {missing}"
