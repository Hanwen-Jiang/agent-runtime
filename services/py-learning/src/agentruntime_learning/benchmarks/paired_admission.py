"""One-off N16/EV-REAL collection permission, not a general budget policy.

These pure checks consume a tenant-scoped authoritative ledger observation.
They cannot attest its origin, settle a reservation, mutate metrics or waive a
quality/cost gate. Call validate_receipt against the unchanged plan first.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

KIND = "paired-collection-continue-v1"
TENANT = "gh-goalpair-20261107"
PENDING = "mbr_DTTAF6WU2EWRW4S267NLH6APYE"
PENDING_MICROS = 14_517
SUBJECT_CAP_MICROS = 18_445_261
RUBRIC_VERSION = "paired-factual-checklist-v1"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _usd(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("a boolean is not an amount")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid amount") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("amount must be finite and nonnegative")
    return amount


def _rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, (list, tuple)):
        raise ValueError("ledger rows must be a sequence")
    result = {}
    for value in rows:
        row = dict(value)
        if row.get("tenant_id", TENANT) != TENANT:
            raise ValueError("cross-tenant ledger row")
        for key in ("reservation_id", "task_id", "node_id", "model_profile_id",
                    "registry_scope", "registry_revision", "registry_document_sha256"):
            if not isinstance(row.get(key), str) or not row[key]:
                raise ValueError("ledger identity is incomplete")
        for key in ("reserved_at", "reserved_usd_micros"):
            if type(row.get(key)) is not int or row[key] < 0:
                raise ValueError("invalid reservation evidence")
        actual = row.get("actual_usd_micros")
        if "actual_usd_micros" not in row or actual is not None and (type(actual) is not int or actual < 0):
            raise ValueError("invalid actual cost")
        rid = row["reservation_id"]
        if rid in result:
            raise ValueError("duplicate reservation")
        result[rid] = row
    return result


def _receipt(receipt: Mapping[str, Any]) -> tuple[dict, dict]:
    if receipt.get("kind") != KIND or receipt.get("tenant_id") != TENANT:
        raise ValueError("wrong collection receipt or isolated tenant")
    base = receipt.get("baseline_rows")
    allowed = receipt.get("allowed_pending")
    if digest(base) != receipt.get("baseline_sha256") or digest(allowed) != receipt.get("allowed_pending_sha256"):
        raise ValueError("receipt ledger digest changed")
    original, pending = _rows(base), _rows(allowed)
    if set(pending) != {PENDING} or pending[PENDING]["reserved_usd_micros"] != PENDING_MICROS:
        raise ValueError("only the explicitly retained reservation and amount are allowed")
    if {k: r for k, r in original.items() if r["actual_usd_micros"] is None} != pending:
        raise ValueError("receipt pending rows differ from the complete baseline")
    if type(receipt.get("subject_cap_usd_micros")) is not int or receipt["subject_cap_usd_micros"] != SUBJECT_CAP_MICROS:
        raise ValueError("subject cap differs from the approved remaining allocation")
    return original, pending


def validate_receipt(plan: Mapping[str, Any], receipt: Mapping[str, Any]) -> None:
    _receipt(receipt)
    bound_plan = dict(plan)
    if "rubric_version" in bound_plan:
        if bound_plan["rubric_version"] != RUBRIC_VERSION:
            raise ValueError("collection manifest changed the fixed rubric")
        bound_plan.pop("rubric_version")
    if plan.get("tenant_id") != TENANT or receipt.get("plan_sha256") not in (digest(plan), digest(bound_plan)):
        raise ValueError("collection receipt does not bind the frozen plan/tenant")
    limits = plan.get("cost_reserves", {})
    cap = _usd(plan.get("budget_usd"))
    if (_usd(limits.get("semantic_judge_usd")) != Decimal("7")
            or _usd(limits.get("external_jev_usd")) != Decimal("12.5")
            or cap != Decimal("37.945261")
            or Decimal(SUBJECT_CAP_MICROS) / 1_000_000 + Decimal("19.5") != cap):
        raise ValueError("frozen allocation changed")
    total, hold, invalid = (_usd(receipt.get(k)) for k in
                            ("original_total_cap_usd", "prior_budget_hold_usd", "prior_invalid_sample_usd"))
    if (total != Decimal("40") or hold != Decimal("1.969152") or invalid != Decimal("0.085586798")
            or cap + hold + invalid > total):
        raise ValueError("original shared allowance or earlier charges changed")
    check_scope(receipt["baseline_rows"], receipt)


def check_scope(rows: Sequence[Mapping[str, Any]], receipt: Mapping[str, Any]) -> dict[str, Any]:
    original, allowed = _receipt(receipt)
    current = _rows(rows)
    if any(current.get(key) != value for key, value in original.items()):
        raise ValueError("baseline ledger row changed or disappeared")
    pending = {k: r for k, r in current.items() if r["actual_usd_micros"] is None}
    if pending != allowed:
        raise ValueError("new or changed pending charge; stop collection")
    known = sum(r["actual_usd_micros"] or 0 for r in current.values())
    retained = sum(r["reserved_usd_micros"] for r in pending.values())
    if known + retained > SUBJECT_CAP_MICROS:
        raise ValueError("subject hard allocation exhausted")
    return {"known_usd_micros": known, "pending_reserved_usd_micros": retained,
            "accounted_usd_micros": known + retained,
            "remaining_usd_micros": SUBJECT_CAP_MICROS - known - retained,
            "cost_known": False, "pending_reservation_ids": sorted(pending)}


def collection_eligible(metric: Mapping[str, Any], task_rows: Sequence[Mapping[str, Any]],
                        receipt: Mapping[str, Any]) -> bool:
    """Recognize complete observations; never rewrite valid=false/cost_known."""
    original, allowed = _receipt(receipt)
    rows = _rows(task_rows)
    task = metric.get("task_id")
    if not rows or any(r["task_id"] != task for r in rows.values()):
        raise ValueError("Task accounting does not match the collected sample")
    if any(rows.get(rid) != row for rid, row in original.items() if row["task_id"] == task):
        raise ValueError("Task accounting omitted or changed a baseline attempt")
    for rid, row in rows.items():
        if rid in original and row != original[rid]:
            raise ValueError("collected baseline attempt changed")
        if row["actual_usd_micros"] is None and allowed.get(rid) != row:
            raise ValueError("unapproved unknown Task charge")
    pending = sum(r["actual_usd_micros"] is None for r in rows.values())
    ledger = metric.get("ledger") or {}
    known = sum(r["actual_usd_micros"] or 0 for r in rows.values())
    if (ledger.get("attempts") != len(rows) or ledger.get("pending") != pending
            or ledger.get("known") is not (pending == 0)
            or _usd(ledger.get("total_cost_usd")) * 1_000_000 != known):
        raise ValueError("Task metric cost evidence disagrees with ledger")
    if (metric.get("status") not in ("done", "failed", "aborted", "cancelled")
            or metric.get("runtime_stable") is not True or metric.get("wall_time_complete") is not True
            or (metric.get("planning") or {}).get("complete") is not True
            or metric.get("unsupported_subtasks") != []
            or any(ledger.get(k) != [] for k in ("registry_mismatch_reservations", "model_mismatch_reservations", "unresolved_role_reservations"))
            or metric.get("elapsed_s") is None):
        return False
    _usd(metric["elapsed_s"])
    return metric.get("valid") is True if not pending else metric.get("valid") is False
