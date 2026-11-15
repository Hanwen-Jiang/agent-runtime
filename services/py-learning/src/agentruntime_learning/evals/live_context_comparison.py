"""Recompute the fixed LongBench cohort view from selected execution records.

``definition`` supplies presentation metadata only. Counts and measurements are
never trusted: they are rebuilt from the allowlisted selected-result paths on
every source refresh. The caller owns safe file access (NOFOLLOW, bounded reads)
and the cache/mtime/checked-at policy. This module performs no writes or model
calls and never exposes source passages, answers, predictions or file paths.

A producer updates its selected-result file when accepting a retry. We do not
pick a result by timestamp, by score, or by searching arbitrary attempt files.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

SCHEMA = "runtime-40-comparison-v1"
COMPARISON_ID = "longbench-context-20261110-cohort40"
PLAN_PATH = "longbench-context-20261110-a/comparison-plan.json"
COUNT = 40
MODEL = "a6api/kimi-k3.1-pro@2026-11"
ITEM_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
MAX_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class RecordSource:
    run: str
    directory: str
    variant: str
    suffix: str

    def path(self, item: str) -> str:
        return f"{self.run}/{self.directory}/{item}{self.suffix}.json"


SOURCES = {
    "mask_only": RecordSource("longbench-context-20261110-limit-repair", "merged-records-v2", "mask_only", "--mask_only"),
    "full": RecordSource("longbench-context-20261110-limit-repair", "merged-records-v2", "full", "--full"),
    "evidence": RecordSource("longbench-context-20261110-optimization", "records/development", "candidate", "--candidate"),
    "uncompressed": RecordSource("longbench-context-20261110-uncompressed40", "records", "uncompressed", ""),
    "source_retrieval": RecordSource("longbench-context-20261110-retrieval-optimization", "records/development", "candidate", "--candidate"),
    "source_focus_v3": RecordSource("longbench-context-20261110-retrieval-v3", "records/development", "candidate", "--candidate"),
}


def _document(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError("invalid live comparison document")
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError("live comparison document must be an object")
    return doc


def _number(value: Any) -> int | float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 2**53 - 1:
        raise ValueError("invalid live comparison measurement")
    return value


def _sum_known(values: list[int | float | None]) -> int | float | None:
    known = [v for v in values if v is not None]
    total = math.fsum(known) if known else None
    if total is not None and total > 2**53 - 1:
        raise ValueError("live comparison aggregate is too large")
    return total


def _text(value: Any, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError("invalid live comparison metadata")
    return value


def _model_evidence(row: Mapping[str, Any]) -> set[str]:
    observed: set[str] = set()
    for field in ("model_profile", "served_model_profile"):
        if row.get(field) is not None:
            observed.add(_text(row[field]))
    profiles = row.get("model_profiles")
    if profiles is not None:
        if not isinstance(profiles, list) or len(profiles) > 32:
            raise ValueError("invalid selected-result model evidence")
        observed.update(_text(p) for p in profiles)
    calls = row.get("calls")
    if calls is not None:
        if not isinstance(calls, list) or len(calls) > 4096:
            raise ValueError("invalid selected-result calls")
        for call in calls:
            if not isinstance(call, dict):
                raise ValueError("invalid selected-result call")
            for field in ("model_profile", "served_model_profile"):
                if call.get(field) is not None:
                    observed.add(_text(call[field]))
    return observed


def _record(row: dict[str, Any], item: str, source: RecordSource, inputs: dict[str, str]) -> dict[str, Any]:
    if row.get("item_id") != item or row.get("variant") != source.variant:
        raise ValueError("selected result does not match the planned item/variant")
    valid, answered, correct = row.get("execution_valid"), row.get("answered"), row.get("correct")
    if type(valid) is not bool or type(answered) is not bool or correct is not None and type(correct) is not bool:
        raise ValueError("selected result has invalid outcome flags")
    if valid and type(correct) is not bool or correct is True and not (valid and answered):
        raise ValueError("selected result scoring flags are inconsistent")
    # A terminal result without an answer remains unscored, even if an old
    # producer marked a strategy-level budget rejection execution_valid=True.
    answer_valid = valid and answered
    observed = _model_evidence(row)
    if answer_valid and observed != {MODEL}:
        raise ValueError("valid selected result lacks the fixed served-model evidence")
    digest = row.get("input_sha256")
    if digest is not None:
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError("selected result has an invalid input digest")
        if item in inputs and inputs[item] != digest:
            raise ValueError("selected results do not share the same item input")
        inputs[item] = digest
    if row.get("cohort_sha256") is not None and not isinstance(row["cohort_sha256"], str):
        raise ValueError("invalid selected-result cohort digest")
    known_cost = _number(row.get("cost_usd"))
    if known_cost is None:
        known_cost = _number(row.get("known_cost_usd"))
    pending = _number(row.get("pending_reserved_usd"))
    tokens = _number(row.get("input_tokens"))
    elapsed = _number(row.get("elapsed_seconds"))
    if row.get("elapsed_known") is False or not answer_valid and elapsed == 0:
        elapsed = None
    if not answer_valid and tokens == 0:
        tokens = None  # historical failed rows contain unknown-value zero placeholders
    return {"valid": answer_valid, "correct": correct is True and answer_valid,
            "cost": known_cost, "pending": pending, "tokens": tokens, "elapsed": elapsed}


def build_live_cohort(read: Callable[[str], bytes], definition: dict[str, Any]) -> dict[str, Any]:
    """Return a rebuilt definition-shaped document, plus a stable source revision.

    ``read`` must reject symlinks/path traversal and enforce size limits. Only
    literal allowlisted locations and validated planned item IDs reach it.
    FileNotFoundError means a selected result has not been produced; malformed
    data raises ValueError so the caller can retain its last good snapshot.

    ``source_revision`` hashes the plan, presentation identity and every selected
    record (including absence markers); it changes when any source result changes.
    It deliberately excludes clocks. The caller sets updated_at from the mtimes
    read in the same refresh, and sets checked_at separately.
    """
    if (not isinstance(definition, dict) or definition.get("schema") != SCHEMA
            or definition.get("id") != COMPARISON_ID or definition.get("comparison_id") != COMPARISON_ID
            or definition.get("sample_count") != COUNT or definition.get("dataset_name") != "LongBench v2"):
        raise ValueError("unsupported live context comparison definition")
    plan_raw = read(PLAN_PATH)
    plan = _document(plan_raw)
    ids = plan.get("item_ids")
    if (not isinstance(ids, list) or len(ids) != COUNT
            or any(not isinstance(i, str) or not ITEM_ID.fullmatch(i) for i in ids)
            or len(set(ids)) != COUNT):
        raise ValueError("live context comparison requires 40 unique safe item IDs")
    for key in ("dataset_version", "runtime_revision", "model_profile", "cohort_sha256"):
        if definition.get(key) != plan.get(key) or not isinstance(plan.get(key), str) or not plan[key]:
            raise ValueError("comparison definition differs from the frozen cohort plan")
    if plan["model_profile"] != MODEL or not SHA256.fullmatch(plan["cohort_sha256"]):
        raise ValueError("unsupported comparison model/cohort")
    variants = definition.get("variants")
    if not isinstance(variants, list) or not 2 <= len(variants) <= len(SOURCES):
        raise ValueError("invalid live comparison variants")
    seen: set[str] = set()
    metadata: list[dict[str, Any]] = []
    for variant in variants:
        if not isinstance(variant, dict):
            raise ValueError("invalid live comparison variant")
        name = variant.get("id")
        if not isinstance(name, str) or name not in SOURCES or name in seen:
            raise ValueError("unregistered or duplicate live comparison variant")
        seen.add(name)
        source = SOURCES[name]
        if (variant.get("source_run_id") != source.run or variant.get("cohort_sha256") != plan["cohort_sha256"]
                or variant.get("model_profile") != MODEL):
            raise ValueError("variant source/model/cohort differs from its registered source")
        metadata.append({"id": name, "source_run_id": source.run,
                         **{key: _text(variant.get(key), 128) for key in ("label_zh", "label_en", "protocol")},
                         "cohort_sha256": plan["cohort_sha256"], "model_profile": MODEL})
    if "mask_only" not in seen:
        raise ValueError("the masking baseline is required")
    revision = hashlib.sha256()
    revision.update(PLAN_PATH.encode() + b"\0" + plan_raw + b"\0")
    revision.update(json.dumps(metadata, sort_keys=True, ensure_ascii=False).encode() + b"\0")
    v3_plan = None
    if "source_focus_v3" in seen:
        v3_plan_path = "longbench-context-20261110-retrieval-v3/development-plan.json"
        v3_raw = read(v3_plan_path)
        v3_plan = _document(v3_raw)
        if (v3_plan.get("phase") != "development" or v3_plan.get("ids") != ids
                or v3_plan.get("model") != MODEL or v3_plan.get("dataset_version") != plan["dataset_version"]):
            raise ValueError("v3 development plan differs from the original shared cohort")
        for key in ("source_sha256", "config_sha256"):
            if not isinstance(v3_plan.get(key), str) or not SHA256.fullmatch(v3_plan[key]):
                raise ValueError("invalid v3 selected-source identity")
        revision.update(v3_plan_path.encode() + b"\0" + v3_raw + b"\0")
    inputs: dict[str, str] = {}
    rebuilt: list[dict[str, Any]] = []
    available = 0
    missing: dict[str, int] = {}
    for meta in metadata:
        source = SOURCES[meta["id"]]
        rows = []
        for item in ids:
            path = source.path(item)
            revision.update(path.encode() + b"\0")
            try:
                raw = read(path)
            except FileNotFoundError:
                revision.update(b"missing\0")
                continue
            revision.update(b"present\0" + raw + b"\0")
            row = _document(raw)
            if meta["id"] == "source_focus_v3" and any(row.get(key) != v3_plan[key] for key in ("source_sha256", "config_sha256")):
                raise ValueError("v3 selected result differs from its frozen source/config")
            if row.get("cohort_sha256") is not None and row["cohort_sha256"] != plan["cohort_sha256"]:
                raise ValueError("selected result has a different cohort")
            rows.append(_record(row, item, source, inputs))
        completed = len(rows)
        valid = sum(r["valid"] for r in rows)
        missing[meta["id"]] = COUNT - completed
        available += completed
        # Missing metrics are unknown, not zero; positive known values still
        # produce lower bounds. Pending totals require an explicit value in all
        # selected records to avoid claiming that unreported reservations are 0.
        pending_values = [r["pending"] for r in rows]
        rebuilt.append({**meta, "planned_count": COUNT, "completed_count": completed,
                        "valid_count": valid, "correct_count": sum(r["correct"] for r in rows),
                        "unscored_count": completed - valid,
                        "status": "completed" if completed == COUNT else "running" if completed else "not_started",
                        "known_cost_usd": _sum_known([r["cost"] for r in rows]),
                        "pending_reserved_usd": _sum_known(pending_values) if rows and all(v is not None for v in pending_values) else None,
                        "recorded_input_tokens": _sum_known([r["tokens"] for r in rows]),
                        "selected_elapsed_seconds": _sum_known([r["elapsed"] for r in rows])})
    return {"schema": SCHEMA, "id": COMPARISON_ID, "comparison_id": COMPARISON_ID,
            "sample_count": COUNT, "dataset_name": "LongBench v2",
            **{k: plan[k] for k in ("dataset_version", "runtime_revision", "model_profile", "cohort_sha256")},
            "updated_at": definition.get("updated_at"), "variants": rebuilt,
            "source_revision": revision.hexdigest(), "source_records_available": available,
            "source_records_expected": COUNT * len(metadata), "source_missing_records": missing}


__all__ = ["build_live_cohort"]
