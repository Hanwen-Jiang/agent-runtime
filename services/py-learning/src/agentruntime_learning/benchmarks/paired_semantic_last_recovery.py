"""One unchanged last-item retry; preserve 21 judgments and all prior charges."""
from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

from agentruntime_learning.benchmarks import paired_semantic_judge as sj, paired_semantic_recovery as first
from agentruntime_learning.benchmarks import paired_semantic_format_recovery as formatted
from agentruntime_learning.benchmarks import paired_semantic_raw_recovery as extracted, provenance as pv

KIND = "paired-semantic-last-known-v1"
ITEM = "fd5dff6d8baa993de772afea"
RESERVATION = "mbr_AKS6JLJO4A5IFTYEHE4KRC7CAK"
COST = 35_290
KNOWN_TOTAL = 1_002_408
PRIOR_KNOWN = 152_277


def _failure(row: dict | None, manifest: dict) -> None:
    expected = {"reservation_id": RESERVATION, "task_id": manifest["task_id"], "node_id": "eval_call_00000026",
                "method": "generate", "model_profile_id": first.PROFILE, "actual_usd_micros": COST,
                "reserved_usd_micros": 344176, "input_tokens": 2136, "output_tokens": 1354,
                "registry_scope": manifest["registry"]["scope"], "registry_revision": manifest["registry"]["revision"],
                "registry_document_sha256": manifest["registry"]["document_sha256"]}
    if (not isinstance(row, dict) or any(row.get(key) != value for key, value in expected.items())
            or any(type(row.get(key)) is not int for key in ("actual_usd_micros", "reserved_usd_micros", "reserved_at", "input_tokens", "output_tokens"))):
        raise sj.JudgeError("the reviewed last failure payment changed")


def _parent(path: Path, proof: dict) -> tuple[dict, dict, dict, dict, list[dict], list[dict]]:
    manifest, plan, export = (first._read(path / name) for name in ("manifest.json", "plan.json", "export.json"))
    previous = first._read(path / "recovery.json")
    if (previous.get("kind") != extracted.KIND or manifest.get("schema_transport_version") != 2
            or manifest.get("legacy_completed_item_ids") != []
            or pv.digest(plan) != manifest.get("plan_sha256") or pv.digest(export) != manifest.get("export_sha256")
            or sj._request_digests(manifest, export) != manifest.get("request_sha256")):
        raise sj.JudgeError("last-item recovery cannot be repeated or change the scoring configuration")
    prior = extracted.validate(path, manifest, previous)
    items = sorted(export["items"], key=lambda item: item["id"])
    calls = first._journal(path / "calls.jsonl")
    if (len(calls) != 39 or items[-1]["id"] != ITEM
            or [row.get("state") for row in calls] != ["completed"] * 5 + ["pending", "completed"] * 16 + ["pending", "failed"]
            or [row.get("id") for row in calls] != [item["id"] for item in items[:5]] + [item["id"] for item in items[5:] for _ in range(2)]
            or calls[-1].get("error_type") != "InvalidError" or (path / "review.json").exists()
            or {p.name for p in path.glob("*.response.json")} != {item["id"] + ".response.json" for item in items[:-1]}):
        raise sj.JudgeError("only the final unreturned format failure may be retried")
    records = [row for row in calls if row["state"] == "completed"]
    for index, (record, item) in enumerate(zip(records, items[:-1])):
        raw, result, cost = extracted._raw(path, item, manifest)
        review, metadata = sj.extract_scores(item, result)
        if (record.get("review") != review or record.get("response_sha256") != pv.digest(raw)
                or record.get("rpc_cost_micros") != cost or record.get("node_id") != f"eval_call_{index + 5:08d}"
                or record.get("raw_schema_valid", True) != metadata["raw_schema_valid"]
                or record.get("ignored_root_fields", []) != metadata["ignored_root_fields"]
                or record.get("result_extraction_version", extracted.EXTRACTION) != extracted.EXTRACTION):
            raise sj.JudgeError("a retained judgment or its schema-failure status changed")
    if sum(row["rpc_cost_micros"] for row in records) != KNOWN_TOTAL - PRIOR_KNOWN:
        raise sj.JudgeError("the 21 retained judgments must keep their complete payments")
    if first._read(path / "budget.json") != {"limit_usd_micros": first.CAP_MICROS - first.HELD_MICROS,
                                               "spent_usd_micros": KNOWN_TOTAL, "pending": True}:
        raise sj.JudgeError("the paid last failure cannot be refunded or cleared")
    identity = first._read(path / "usage/identity.json")
    if (identity.get("run_id") != manifest["run_id"] or identity.get("task_id") != manifest["task_id"]
            or identity.get("tenant_id") != first.TENANT or identity.get("gateway_address") != manifest["gateway_address"]
            or identity.get("sequence_offset") != 9):
        raise sj.JudgeError("parent audit identity changed")
    audit = first._journal(path / "usage/calls.jsonl")
    if (len(audit) != 34 or [row.get("sequence") for row in audit] != [n for n in range(10, 27) for _ in range(2)]
            or [row.get("state") for row in audit] != ["pending", "completed"] * 16 + ["pending", "unresolved"]
            or audit[-1].get("error_type") != "InvalidError" or audit[-1].get("known_cost_usd_micros") != COST):
        raise sj.JudgeError("the last known failure lacks its exact audit evidence")
    costs = [row["rpc_cost_micros"] for row in records[5:]] + [COST]
    for sequence, item, cost, pair in zip(range(10, 27), items[5:], costs, zip(audit[::2], audit[1::2])):
        if (any(row.get("task_id") != manifest["task_id"] or row.get("node_id") != f"eval_call_{sequence:08d}"
                or row.get("source_node_id") != "semantic_" + item["id"] for row in pair)
                or pair[1].get("known_cost_usd_micros") != cost):
            raise sj.JudgeError("parent audit costs disagree with the retained judgments")
    rows = proof.get("attempts", proof.get("rows", []))
    if (proof.get("tenant_id") != first.TENANT or proof.get("task_id", manifest["task_id"]) != manifest["task_id"]
            or len(rows) != 26 or len({row["reservation_id"] for row in rows}) != 26):
        raise sj.JudgeError("last recovery requires all 26 prior ledger attempts")
    baseline = first._read(path / "parent-ledger.json")["attempts"]
    observed = {row["reservation_id"]: row for row in rows}
    if any(observed.get(row["reservation_id"]) != row for row in baseline):
        raise sj.JudgeError("a prior original ledger row changed")
    failed = next((row for row in rows if row["reservation_id"] == RESERVATION), None)
    _failure(failed, manifest)
    known = [*prior["retained_prior_known"], failed]
    sj._ledger({"tenant_id": first.TENANT, "task_id": manifest["task_id"], "attempts": rows}, manifest,
               {row["id"]: row for row in records}, KNOWN_TOTAL, prior["retained_pending"], known)
    return manifest, plan, export, prior, records, known


def _estimate(manifest: dict, export: dict) -> Decimal:
    return formatted._estimate(manifest, {**export, "items": sorted(export["items"], key=lambda item: item["id"])[-1:]})


def prepare(parent: Path, out: Path, proof: dict) -> dict:
    parent, out = Path(parent).resolve(), Path(out).resolve()
    if parent == out or out.exists():
        raise sj.JudgeError("last recovery requires a separate new directory")
    manifest, plan, export, prior, records, known = _parent(parent, proof)
    estimate = _estimate(manifest, export)
    if estimate + Decimal(KNOWN_TOTAL + first.HELD_MICROS) / 1_000_000 > 7:
        raise sj.JudgeError("the last item and all prior payments exceed the original allowance")
    copied = sj.prepare(plan, export, out, tenant_id=manifest["tenant_id"], gateway_address=manifest["gateway_address"],
                        tier=manifest["tier"], max_output_tokens=12000, thinking_budget_tokens=8192, schema_transport_version=2)
    if copied != manifest:
        raise sj.JudgeError("last recovery changed the scoring requests")
    for row in records:
        shutil.copy2(parent / (row["id"] + ".response.json"), out / (row["id"] + ".response.json"))
        sj._append(out / "calls.jsonl", row)
    sj._write(out / "parent-ledger.json", {"tenant_id": first.TENANT, "task_id": manifest["task_id"],
        "attempts": proof.get("attempts", proof.get("rows", []))})
    receipt = {"kind": KIND, "original_directory": str(parent), "parent_files": extracted._files(parent),
        "preserved_directories": [str(parent), *prior["preserved_directories"]], "manifest_sha256": pv.digest(manifest),
        "parent_ledger_sha256": first._hash(out / "parent-ledger.json"), "seed_records": records,
        "retained_pending": prior["retained_pending"], "retained_prior_known": known, "sequence_offset": 26,
        "original_cap_usd_micros": first.CAP_MICROS, "held_usd_micros": first.HELD_MICROS,
        "execution_cap_usd_micros": first.CAP_MICROS - first.HELD_MICROS, "prior_known_usd_micros": PRIOR_KNOWN,
        "original_spent_usd_micros": KNOWN_TOTAL, "retry_item_id": ITEM, "last_item_retry_limit": 1,
        "result_extraction_version": extracted.EXTRACTION, "remaining_items": 1,
        "remaining_estimate_usd": str(estimate), "actual_total_cost_known": False}
    sj._write(out / "recovery.json", receipt)
    return receipt


def validate(out: Path, manifest: dict, receipt: dict) -> dict:
    out, parent = Path(out).resolve(), Path(receipt["original_directory"]).resolve()
    if (out == parent or receipt.get("kind") != KIND or extracted._files(parent) != receipt.get("parent_files")
            or first._hash(out / "parent-ledger.json") != receipt.get("parent_ledger_sha256")):
        raise sj.JudgeError("original last-item evidence changed")
    previous, _, export, prior, records, known = _parent(parent, first._read(out / "parent-ledger.json"))
    fixed = {"sequence_offset": 26, "original_cap_usd_micros": first.CAP_MICROS, "held_usd_micros": first.HELD_MICROS,
             "execution_cap_usd_micros": first.CAP_MICROS - first.HELD_MICROS, "prior_known_usd_micros": PRIOR_KNOWN,
             "original_spent_usd_micros": KNOWN_TOTAL, "remaining_items": 1, "last_item_retry_limit": 1}
    if (previous != manifest or receipt.get("manifest_sha256") != pv.digest(manifest)
            or any(type(receipt.get(key)) is not int or receipt[key] != value for key, value in fixed.items())
            or receipt.get("retained_pending") != prior["retained_pending"] or receipt.get("retained_prior_known") != known
            or receipt.get("preserved_directories") != [str(parent), *prior["preserved_directories"]]
            or receipt.get("seed_records") != records or receipt.get("retry_item_id") != ITEM
            or receipt.get("result_extraction_version") != extracted.EXTRACTION or receipt.get("actual_total_cost_known") is not False):
        raise sj.JudgeError("last recovery changed the frozen scoring conditions or accounting")
    if first._journal(out / "calls.jsonl")[:21] != records:
        raise sj.JudgeError("the 21 retained judgments changed")
    for row in records:
        name = row["id"] + ".response.json"
        if (out / name).read_bytes() != (parent / name).read_bytes():
            raise sj.JudgeError("a retained raw response changed")
    estimate = Decimal(receipt["remaining_estimate_usd"])
    if (not estimate.is_finite() or estimate != _estimate(manifest, export)
            or estimate + Decimal(KNOWN_TOTAL + first.HELD_MICROS) / 1_000_000 > 7):
        raise sj.JudgeError("last recovery exceeds the original allowance")
    return receipt
