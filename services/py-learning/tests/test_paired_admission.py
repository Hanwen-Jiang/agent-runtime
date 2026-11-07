from __future__ import annotations

import copy
import json
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


def successor_fixture():
    plan, before, _, metric = fixture()
    after = copy.deepcopy(before)
    second = dict(before["allowed_pending"][0], reservation_id=admission.SECOND_PENDING,
                  task_id="rules-first-task", reserved_usd_micros=9083)
    after["baseline_rows"].append(copy.deepcopy(second))
    after["allowed_pending"].append(copy.deepcopy(second))
    after["previous_receipt_sha256"] = admission.digest(before)
    rehash(after)
    return plan, before, after, metric


def rehash(receipt):
    receipt["baseline_sha256"] = admission.digest(receipt["baseline_rows"])
    receipt["allowed_pending_sha256"] = admission.digest(receipt["allowed_pending"])


def test_exact_second_pending_is_a_monotonic_cost_unknown_successor():
    plan, before, after, metric = successor_fixture()
    untouched = copy.deepcopy((plan, before, after, metric))
    admission.validate_successor(plan, before, after)
    assert admission.check_scope(before["baseline_rows"], before)["pending_reserved_usd_micros"] == 14517
    totals = admission.check_scope(after["baseline_rows"], after)
    assert totals["pending_reserved_usd_micros"] == 23600
    assert totals["accounted_usd_micros"] == 119361 + 23600
    assert totals["remaining_usd_micros"] == 18445261 - 119361 - 23600
    assert totals["cost_known"] is False
    assert admission.collection_eligible(metric, before["baseline_rows"], after)
    second_metric = copy.deepcopy(metric)
    second_metric["task_id"] = "rules-first-task"
    second_metric["ledger"].update(attempts=1, total_cost_usd=0)
    assert admission.collection_eligible(second_metric, [after["baseline_rows"][-1]], after)
    assert second_metric["valid"] is False and second_metric["ledger"]["known"] is False
    assert (plan, before, after, metric) == untouched
    with pytest.raises(ValueError, match="new or changed pending"):
        admission.check_scope(after["baseline_rows"], before)


@pytest.mark.parametrize("change", ["remove_old_pending", "settle_old_pending", "change_known", "remove_known",
    "change_old_pending", "second_amount", "third_id", "new_tenant", "subject_cap", "total_cap",
    "refund_hold", "refund_prior", "plan", "predecessor", "missing_predecessor"])
def test_successor_cannot_rewrite_evidence_release_money_or_add_another_exception(change):
    plan, before, after, _ = successor_fixture()
    if change in ("remove_old_pending", "settle_old_pending"):
        after["allowed_pending"].pop(0)
        if change == "remove_old_pending":
            after["baseline_rows"].pop(1)
        else:
            after["baseline_rows"][1]["actual_usd_micros"] = 0
    elif change == "change_known":
        after["baseline_rows"][0]["actual_usd_micros"] -= 1
    elif change == "remove_known":
        after["baseline_rows"].pop(0)
    elif change == "change_old_pending":
        after["baseline_rows"][1]["reserved_at"] += 1
        after["allowed_pending"][0]["reserved_at"] += 1
    elif change == "second_amount":
        after["baseline_rows"][-1]["reserved_usd_micros"] -= 1
        after["allowed_pending"][-1]["reserved_usd_micros"] -= 1
    elif change == "third_id":
        row = dict(after["allowed_pending"][-1], reservation_id="unapproved-third")
        after["baseline_rows"].append(row)
        after["allowed_pending"].append(row)
    else:
        key, value = {"new_tenant": ("tenant_id", "another"), "subject_cap": ("subject_cap_usd_micros", 18445262),
            "total_cap": ("original_total_cap_usd", "41"), "refund_hold": ("prior_budget_hold_usd", "0"),
            "refund_prior": ("prior_invalid_sample_usd", "0"), "plan": ("plan_sha256", "a" * 64),
            "predecessor": ("previous_receipt_sha256", "a" * 64),
            "missing_predecessor": ("previous_receipt_sha256", None)}[change]
        after[key] = value
    rehash(after)
    with pytest.raises(ValueError):
        admission.validate_successor(plan, before, after)


def test_successor_rejects_rollback_and_noop_even_with_a_new_chain_digest():
    plan, before, after, _ = successor_fixture()
    admission.validate_successor(plan, before, after)
    rollback = copy.deepcopy(before)
    rollback["previous_receipt_sha256"] = admission.digest(after)
    with pytest.raises(ValueError):
        admission.validate_successor(plan, after, rollback)
    unchanged = copy.deepcopy(after)
    unchanged["previous_receipt_sha256"] = admission.digest(after)
    with pytest.raises(ValueError, match="only the exact second"):
        admission.validate_successor(plan, after, unchanged)


def test_receipt_storage_appends_without_rewriting_and_is_idempotent(tmp_path):
    plan, before, after, _ = successor_fixture()
    assert admission.stored_receipt(tmp_path, plan) is None
    assert admission.store_receipt(tmp_path, plan, before) == before
    original = tmp_path / "collection-continue.json"
    before_bytes, before_mtime = original.read_bytes(), original.stat().st_mtime_ns
    assert admission.store_receipt(tmp_path, plan, before) == before
    assert admission.store_receipt(tmp_path, plan, after) == after
    extension = tmp_path / "collection-continue-extensions" / (admission.digest(after) + ".json")
    extension_bytes, extension_mtime = extension.read_bytes(), extension.stat().st_mtime_ns
    assert admission.stored_receipt(tmp_path, plan) == after
    assert admission.store_receipt(tmp_path, plan, after) == after
    assert original.read_bytes() == before_bytes and original.stat().st_mtime_ns == before_mtime
    assert extension.read_bytes() == extension_bytes and extension.stat().st_mtime_ns == extension_mtime
    assert list(extension.parent.glob("*.json")) == [extension]


def test_existing_root_json_format_remains_byte_identical(tmp_path):
    plan, before, after, _ = successor_fixture()
    original = tmp_path / "collection-continue.json"
    raw = json.dumps(before, separators=(",", ":")) + "\n\n"
    original.write_text(raw)
    admission.store_receipt(tmp_path, plan, after)
    assert original.read_text() == raw


def test_extension_cannot_be_stored_as_a_root_or_roll_back_the_leaf(tmp_path):
    plan, before, after, _ = successor_fixture()
    with pytest.raises(ValueError, match="original single-charge"):
        admission.store_receipt(tmp_path, plan, after)
    assert not (tmp_path / "collection-continue.json").exists()
    admission.store_receipt(tmp_path, plan, before)
    admission.store_receipt(tmp_path, plan, after)
    with pytest.raises(ValueError):
        admission.store_receipt(tmp_path, plan, before)
    assert admission.stored_receipt(tmp_path, plan) == after


def test_store_rejects_fork_and_reader_detects_an_externally_added_fork(tmp_path):
    plan, before, after, _ = successor_fixture()
    admission.store_receipt(tmp_path, plan, before)
    admission.store_receipt(tmp_path, plan, after)
    fork = {**after, "review_note": "a different receipt with the same predecessor"}
    with pytest.raises(ValueError):
        admission.store_receipt(tmp_path, plan, fork)
    assert admission.stored_receipt(tmp_path, plan) == after
    path = tmp_path / "collection-continue-extensions" / (admission.digest(fork) + ".json")
    path.write_text(json.dumps(fork))
    with pytest.raises(ValueError, match="forked"):
        admission.stored_receipt(tmp_path, plan)


@pytest.mark.parametrize("change", ["orphan", "missing_root", "renamed", "tampered", "changed_root"])
def test_loading_receipts_rejects_orphans_and_changed_evidence(tmp_path, change):
    plan, before, after, _ = successor_fixture()
    admission.store_receipt(tmp_path, plan, before)
    admission.store_receipt(tmp_path, plan, after)
    original = tmp_path / "collection-continue.json"
    path = tmp_path / "collection-continue-extensions" / (admission.digest(after) + ".json")
    if change == "missing_root":
        original.unlink()
    elif change == "renamed":
        path.rename(path.with_name("0" * 64 + ".json"))
    elif change == "tampered":
        path.write_text(json.dumps({**after, "added_after_signoff": True}))
    elif change == "changed_root":
        before["baseline_rows"][0]["actual_usd_micros"] -= 1
        rehash(before)
        original.write_text(json.dumps(before))
    else:
        path.unlink()
        after["previous_receipt_sha256"] = "f" * 64
        path.with_name(admission.digest(after) + ".json").write_text(json.dumps(after))
    with pytest.raises(ValueError):
        admission.stored_receipt(tmp_path, plan)
