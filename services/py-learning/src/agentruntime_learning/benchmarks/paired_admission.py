"""One-off N16/EV-REAL collection permission, not a general budget policy.

These pure checks consume a tenant-scoped authoritative ledger observation.
They cannot attest its origin, settle a reservation, mutate metrics or waive a
quality/cost gate. Call validate_receipt against the unchanged plan first.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

KIND = "paired-collection-continue-v1"
TENANT = "gh-goalpair-20261107"
PENDING = "mbr_DTTAF6WU2EWRW4S267NLH6APYE"
PENDING_MICROS = 14_517
SECOND_PENDING = "mbr_QXBAQLBWMIB2BSAG4U5B33M2SJ"
SECOND_PENDING_MICROS = 9_083
THIRD_PENDING = "mbr_UZORZ3YUTV5V7ELFMW643HXTZM"
THIRD_PENDING_MICROS = 9_214
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
    amounts = {PENDING: PENDING_MICROS, SECOND_PENDING: SECOND_PENDING_MICROS, THIRD_PENDING: THIRD_PENDING_MICROS}
    if (set(pending) not in ({PENDING}, {PENDING, SECOND_PENDING}, set(amounts))
            or any(row["reserved_usd_micros"] != amounts[key] for key, row in pending.items())):
        raise ValueError("only the explicitly retained reservation and amount are allowed")
    if SECOND_PENDING in pending:
        previous = receipt.get("previous_receipt_sha256")
        if not isinstance(previous, str) or len(previous) != 64 or any(c not in "0123456789abcdef" for c in previous):
            raise ValueError("additional retained charges require a predecessor receipt digest")
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


def validate_successor(plan: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    """Validate one reviewed charge at a time; do not activate or write it."""
    validate_receipt(plan, before)
    validate_receipt(plan, after)
    if after.get("previous_receipt_sha256") != digest(before):
        raise ValueError("successor does not bind the exact predecessor receipt")
    fixed = ("kind", "tenant_id", "plan_sha256", "subject_cap_usd_micros",
             "original_total_cap_usd", "prior_budget_hold_usd", "prior_invalid_sample_usd")
    if any(after.get(key) != before.get(key) for key in fixed):
        raise ValueError("successor changed the original plan, tenant or allocation")
    original, old_pending = _receipt(before)
    current, new_pending = _receipt(after)
    if any(current.get(key) != row for key, row in original.items()):
        raise ValueError("successor removed or changed a baseline ledger row")
    transitions = (({PENDING}, {PENDING, SECOND_PENDING}),
                   ({PENDING, SECOND_PENDING}, {PENDING, SECOND_PENDING, THIRD_PENDING}))
    if (set(old_pending), set(new_pending)) not in transitions:
        raise ValueError("successor may append only the exact next retained charge once")


def _root_receipt(plan: Mapping[str, Any], receipt: Mapping[str, Any]) -> None:
    validate_receipt(plan, receipt)
    if "previous_receipt_sha256" in receipt or set(_receipt(receipt)[1]) != {PENDING}:
        raise ValueError("the original single-charge receipt must exist before any extension")


def stored_receipt(out: Path, plan: Mapping[str, Any]) -> dict[str, Any] | None:
    """Load the untouched root and its unique, verified append-only chain."""
    out = Path(out)
    root = out / "collection-continue.json"
    paths = sorted((out / "collection-continue-extensions").glob("*.json"))
    if not root.exists():
        if paths:
            raise ValueError("orphan receipt extensions without the original receipt")
        return None
    current = json.loads(root.read_text("utf-8"))
    _root_receipt(plan, current)
    successors = {}
    for path in paths:
        receipt = json.loads(path.read_text("utf-8"))
        if path.name != digest(receipt) + ".json":
            raise ValueError("receipt filename and content digest disagree")
        validate_receipt(plan, receipt)
        previous = receipt.get("previous_receipt_sha256")
        if previous in successors:
            raise ValueError("forked receipt extensions")
        successors[previous] = receipt
    while digest(current) in successors:
        following = successors.pop(digest(current))
        validate_successor(plan, current, following)
        current = following
    if successors:
        raise ValueError("orphan receipt extension does not descend from the original")
    return current


def _create_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.link(temporary, path)  # Atomic publish; never replace existing evidence.
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


def store_receipt(out: Path, plan: Mapping[str, Any], receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Persist a root or one reviewed extension; preserve all prior bytes."""
    import fcntl

    out = Path(out)
    validate_receipt(plan, receipt)
    out.mkdir(parents=True, exist_ok=True)
    with (out / ".collection-continue.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = stored_receipt(out, plan)
        if current == receipt:
            return current
        if current is None:
            _root_receipt(plan, receipt)
            destination = out / "collection-continue.json"
        else:
            validate_successor(plan, current, receipt)
            destination = out / "collection-continue-extensions" / (digest(receipt) + ".json")
        _create_receipt(destination, receipt)
        return dict(receipt)


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
