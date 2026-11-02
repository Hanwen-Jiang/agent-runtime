"""S-01 preflight inventory for the R-02/R-03 cost and context slice."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "contract" / "fixtures" / "r02_r03_cost_context_contract_cases.json"
BASELINE = ROOT / "docs" / "plan" / "r02-r03-cost-context-contract-baseline-2026-11-03.md"


def _data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_r02_r03_inventory_is_unique_and_covers_required_boundaries() -> None:
    data = _data()
    cases = data["cases"]
    ids = [case["id"] for case in cases]
    assert data["schema"] == "r02_r03_cost_context_contract_cases"
    assert data["version"] == 1
    assert len(ids) == len(set(ids))
    assert {case["category"] for case in cases} >= {"cost", "context", "object", "privacy", "compatibility", "complexity"}
    assert {case["expect"] for case in cases} == {"allow", "deny", "review"}
    assert all(case["assertion"].strip() for case in cases)


def test_r02_r03_inventory_is_grounded_in_candidate_baseline() -> None:
    baseline = BASELINE.read_text(encoding="utf-8")
    for term in (
        "max_cost_usd_micros",
        "Context Builder",
        "obj://<tenant>/<key>",
        "tenant_budget_exceeded",
        "原子预留",
        "不新增上下文缓存表",
    ):
        assert term in baseline, term
