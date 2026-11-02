"""S-01 preflight inventory for the open-signup model budget.

These cases intentionally describe the contract before schema/proto changes.
They prevent the implementation slice from silently dropping accounting,
concurrency, privacy, or task-admission requirements.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "tests" / "contract" / "fixtures" / "model_budget_contract_cases.json"
BASELINE = ROOT / "docs" / "plan" / "model-budget-contract-baseline.md"
OPEN_SIGNUP = ROOT / "docs" / "plan" / "open-signup.md"


def _data() -> dict:
    return json.loads(CASES.read_text(encoding="utf-8"))


def test_model_budget_inventory_is_unique_and_complete() -> None:
    data = _data()
    cases = data["cases"]
    ids = [case["id"] for case in cases]
    assert data["schema"] == "model_budget_contract_cases"
    assert data["version"] == 1
    assert len(ids) == len(set(ids))
    assert {case["area"] for case in cases} == {
        "policy", "accounting", "concurrency", "enforcement", "task_admission",
        "privacy", "failure", "defaults",
    }
    assert {case["expect"] for case in cases} == {"allow", "deny", "review"}
    assert all(case["assertion"].strip() for case in cases)


def test_model_budget_inventory_is_grounded_in_the_candidate_docs() -> None:
    baseline = BASELINE.read_text(encoding="utf-8")
    open_signup = OPEN_SIGNUP.read_text(encoding="utf-8")
    for term in (
        "model_budget.daily_usd",
        "model_budget.monthly_usd",
        "tenant_budget_exceeded",
        "UTC",
        "每次模型调用",
        "第二份 Worker 账本",
    ):
        assert term in baseline, term
    assert "每日/月度模型预算" in open_signup
