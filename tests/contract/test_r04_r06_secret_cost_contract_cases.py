"""S-01/S-02 preflight inventory for R-04 SecretStore and R-06 cost/release semantics."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "contract" / "fixtures" / "r04_r06_secret_cost_contract_cases.json"
BASELINE = ROOT / "docs" / "plan" / "r04-r06-secret-cost-contract-baseline-2026-11-03.md"


def _data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_r04_r06_inventory_is_unique_and_complete() -> None:
    data = _data()
    cases = data["cases"]
    ids = [case["id"] for case in cases]
    assert data["schema"] == "r04_r06_secret_cost_contract_cases"
    assert data["version"] == 1
    assert len(ids) == len(set(ids))
    assert {case["area"] for case in cases} >= {
        "secret_boundary", "privacy", "redaction", "rotation", "cost", "isolation",
        "registry", "release", "learning", "compatibility", "complexity",
    }
    assert {case["expect"] for case in cases} == {"allow", "deny", "review"}
    assert all(case["assertion"].strip() for case in cases)


def test_r04_r06_inventory_is_anchored_in_candidate_baseline() -> None:
    baseline = BASELINE.read_text(encoding="utf-8")
    for anchor in (
        "生产 Provider",
        "写入前脱敏",
        "批量撤销与轮换",
        "唯一成本事件",
        "价格版本固定",
        "Registry 追加版本 + rollback",
        "Learning 评测发布门禁",
        "第二份成本状态机/后台扫描",
        "S-01/S-02 进入实现前门槛",
    ):
        assert anchor in baseline, anchor
