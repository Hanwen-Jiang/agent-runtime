from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from agentruntime_learning.benchmarks import paired_admission as admission


def fixture():
    plan = {"tenant_id": admission.TENANT, "budget_usd": 37.945261,
            "cost_reserves": {"semantic_judge_usd": 7, "external_jev_usd": 12.5}}
    pending = {"reservation_id": admission.PENDING, "task_id": "task-pending",
               "node_id": "compose.1", "model_profile_id": "a6api/deepseek-v4-flash@2026-10",
               "reserved_at": 1794078260490, "reserved_usd_micros": 14517,
               "actual_usd_micros": None, "registry_scope": "platform",
               "registry_revision": "v2", "registry_document_sha256": "a" * 64}
    known = dict(pending, reservation_id="known", actual_usd_micros=119361,
                 reserved_usd_micros=120000)
    rows = [known, pending]
    receipt = {"kind": admission.KIND, "tenant_id": admission.TENANT,
               "plan_sha256": admission.digest(plan), "baseline_rows": copy.deepcopy(rows),
               "baseline_sha256": admission.digest(rows), "allowed_pending": [copy.deepcopy(pending)],
               "allowed_pending_sha256": admission.digest([pending]),
               "subject_cap_usd_micros": 18445261, "original_total_cap_usd": "40",
               "prior_budget_hold_usd": "1.969152", "prior_invalid_sample_usd": "0.085586798"}
    metric = {"task_id": "task-pending", "status": "done", "elapsed_s": 222.1,
              "runtime_stable": True, "wall_time_complete": True, "unsupported_subtasks": [],
              "planning": {"complete": True}, "valid": False,
              "ledger": {"attempts": 2, "pending": 1, "known": False, "total_cost_usd": .119361,
                         "registry_mismatch_reservations": [], "model_mismatch_reservations": [],
                         "unresolved_role_reservations": []}}
    return plan, receipt, rows, metric


def test_complete_unknown_observation_remains_unknown_and_immutable():
    plan, receipt, rows, metric = fixture()
    before = copy.deepcopy((plan, receipt, rows, metric))
    admission.validate_receipt(plan, receipt)
    result = admission.check_scope(rows, receipt)
    assert result == {"known_usd_micros": 119361, "pending_reserved_usd_micros": 14517,
                      "accounted_usd_micros": 133878, "remaining_usd_micros": 18311383,
                      "cost_known": False, "pending_reservation_ids": [admission.PENDING]}
    assert admission.collection_eligible(metric, rows, receipt)
    assert (plan, receipt, rows, metric) == before
    assert metric["valid"] is False and metric["ledger"]["known"] is False


def test_manifest_may_add_only_the_exact_fixed_rubric_marker():
    plan, receipt, _, _ = fixture()
    admission.validate_receipt(plan, receipt)
    manifest = dict(plan, rubric_version="paired-factual-checklist-v1")
    admission.validate_receipt(manifest, receipt)
    manifest["rubric_version"] = "relaxed-rubric"
    with pytest.raises(ValueError, match="rubric"):
        admission.validate_receipt(manifest, receipt)
    manifest["rubric_version"] = "paired-factual-checklist-v1"
    manifest["other_runner_field"] = True
    with pytest.raises(ValueError, match="frozen plan"):
        admission.validate_receipt(manifest, receipt)


@pytest.mark.parametrize("change", ["plan", "tenant", "digest", "pending_digest", "cap", "hold", "total", "prior_cost"])
def test_receipt_rejects_changed_authority_or_budget(change):
    plan, receipt, _, _ = fixture()
    if change == "plan":
        plan["new_prompt"] = "different experiment"
    elif change == "tenant":
        receipt["tenant_id"] = "another-tenant"
    elif change == "digest":
        receipt["baseline_rows"][0]["actual_usd_micros"] += 1
    elif change == "pending_digest":
        receipt["allowed_pending"][0]["reserved_at"] += 1
    else:
        key, value = {"cap": ("subject_cap_usd_micros", 18445261.0),
                      "hold": ("prior_budget_hold_usd", "0"),
                      "total": ("original_total_cap_usd", "40.000001"),
                      "prior_cost": ("prior_invalid_sample_usd", "0.085586797")}[change]
        receipt[key] = value
    with pytest.raises(ValueError):
        admission.validate_receipt(plan, receipt)


@pytest.mark.parametrize("value", [14516, 14518, 14517.0, True])
def test_pending_amount_is_exact_even_after_rehash(value):
    plan, receipt, _, _ = fixture()
    receipt["baseline_rows"][1]["reserved_usd_micros"] = value
    receipt["allowed_pending"][0]["reserved_usd_micros"] = value
    receipt["baseline_sha256"] = admission.digest(receipt["baseline_rows"])
    receipt["allowed_pending_sha256"] = admission.digest(receipt["allowed_pending"])
    with pytest.raises(ValueError):
        admission.validate_receipt(plan, receipt)


@pytest.mark.parametrize("change", ["changed_known", "changed_pending", "missing", "settled", "new_pending", "cross_tenant", "duplicate", "fractional", "over_cap"])
def test_current_scope_requires_unchanged_baseline_and_only_allowed_pending(change):
    _, receipt, rows, _ = fixture()
    if change == "changed_known":
        rows[0]["actual_usd_micros"] -= 1
    elif change == "changed_pending":
        rows[1]["reserved_at"] += 1
    elif change == "missing":
        rows.pop(0)
    elif change == "settled":
        rows[1]["actual_usd_micros"] = 0
    elif change == "new_pending":
        rows.append(dict(rows[1], reservation_id="new-unknown"))
    elif change == "cross_tenant":
        rows.append(dict(rows[0], reservation_id="other", tenant_id="other"))
    elif change == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif change == "fractional":
        rows.append(dict(rows[0], reservation_id="fraction", actual_usd_micros=.1))
    elif change == "over_cap":
        rows.append(dict(rows[0], reservation_id="over", actual_usd_micros=18311384))
    with pytest.raises(ValueError):
        admission.check_scope(rows, receipt)


def test_new_known_charges_can_reach_but_not_exceed_exact_cap():
    _, receipt, rows, _ = fixture()
    rows.append(dict(rows[0], reservation_id="new-known", actual_usd_micros=18311383))
    result = admission.check_scope(rows, receipt)
    assert result["remaining_usd_micros"] == 0 and result["cost_known"] is False
    assert Decimal("37.945261") + Decimal("1.969152") + Decimal("0.085586798") < Decimal("40")


def test_fully_known_valid_metric_is_eligible_without_changing_scope_unknown():
    _, receipt, rows, metric = fixture()
    task_rows = [dict(rows[0], reservation_id="new-known-task-attempt", task_id="fully-known-task")]
    metric.update(valid=True, task_id="fully-known-task")
    metric["ledger"].update(attempts=1, pending=0, known=True)
    assert admission.collection_eligible(metric, task_rows, receipt)
    metric["valid"] = False
    assert not admission.collection_eligible(metric, task_rows, receipt)
    assert not admission.check_scope(rows, receipt)["cost_known"]


def test_metric_cannot_become_known_by_omitting_the_baseline_pending_attempt():
    _, receipt, rows, metric = fixture()
    metric["valid"] = True
    metric["ledger"].update(attempts=1, pending=0, known=True)
    with pytest.raises(ValueError, match="baseline attempt"):
        admission.collection_eligible(metric, rows[:1], receipt)


@pytest.mark.parametrize("field,value", [("runtime_stable", False), ("wall_time_complete", False),
    ("status", "running"), ("planning", {"complete": False}), ("unsupported_subtasks", ["other"]),
    ("elapsed_s", None), ("valid", True)])
def test_incomplete_or_invalid_quality_is_never_waived(field, value):
    _, receipt, rows, metric = fixture()
    metric[field] = value
    assert not admission.collection_eligible(metric, rows, receipt)


@pytest.mark.parametrize("field", ["registry_mismatch_reservations", "model_mismatch_reservations", "unresolved_role_reservations"])
def test_mismatch_or_missing_evidence_rejects_collection(field):
    _, receipt, rows, metric = fixture()
    metric["ledger"][field] = ["known"]
    assert not admission.collection_eligible(metric, rows, receipt)
    del metric["ledger"][field]
    assert not admission.collection_eligible(metric, rows, receipt)


@pytest.mark.parametrize("field,value", [("attempts", 1), ("pending", 0), ("known", True), ("total_cost_usd", "0.119361000001")])
def test_metric_accounting_must_match_without_rounding(field, value):
    _, receipt, rows, metric = fixture()
    metric["ledger"][field] = value
    with pytest.raises(ValueError):
        admission.collection_eligible(metric, rows, receipt)


def test_task_receipt_cannot_hide_new_unknown_or_cross_task_charge():
    _, receipt, rows, metric = fixture()
    for row in (dict(rows[1], reservation_id="new-unknown"), dict(rows[0], task_id="other-task")):
        with pytest.raises(ValueError):
            admission.collection_eligible(metric, [row], receipt)
