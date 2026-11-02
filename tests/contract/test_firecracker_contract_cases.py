"""D-19~D-22/D-26 pre-implementation acceptance inventory."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "tests" / "contract" / "fixtures" / "firecracker_contract_cases.json"
PLAN = ROOT / "docs" / "plan" / "firecracker-backend.md"


def _data() -> dict:
    return json.loads(CASES.read_text(encoding="utf-8"))


def test_firecracker_inventory_is_unique_and_covers_boundaries() -> None:
    data = _data()
    cases = data["cases"]
    ids = [case["id"] for case in cases]
    assert data["schema"] == "firecracker_contract_cases"
    assert data["version"] == 1
    assert len(ids) == len(set(ids))
    assert {case["area"] for case in cases} == {"backend_registry", "isolation", "manager", "rootfs", "vsock", "snapshot", "external"}
    assert {case["expect"] for case in cases} == {"allow", "deny", "blocked_external"}
    assert all(case["assertion"].strip() for case in cases)


def test_inventory_is_grounded_in_firecracker_plan() -> None:
    plan = PLAN.read_text(encoding="utf-8")
    required_terms = ("enabled", "capabilities", "不自动降级", "fc-manager", "vsock", "rootfs", "快照", "/dev/kvm", "D-26")
    missing = [term for term in required_terms if term not in plan]
    assert not missing, f"Firecracker cases no longer match the plan: {missing}"
