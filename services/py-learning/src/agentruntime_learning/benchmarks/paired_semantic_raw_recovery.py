"""Extract an already billed result without changing its raw schema verdict.

The strict cohort, requests and original files remain unchanged. Four completed
judgments and the existing fifth response seed a new directory; no item is
reissued by this preparation step and every old charge remains accounted.
"""
from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path

from agentruntime_learning.benchmarks import paired_semantic_judge as sj, paired_semantic_recovery as first
from agentruntime_learning.benchmarks import paired_semantic_format_recovery as formatted, provenance as pv

KIND = "paired-semantic-raw-extraction-v1"
ITEM = "360c0e00dbf0eba926bc86a6"
RESERVATION = "mbr_B2HTLJ5FXPDTZXZQQYXU76C5GE"
COST = 57_578
KNOWN_TOTAL = 332_277
EXTRACTION = "paired-score-extraction-v1"


def _files(path: Path) -> dict[str, str]:
    return {str(file.relative_to(path)): first._hash(file) for file in sorted(path.rglob("*"))
            if file.is_file() and file.suffix in (".json", ".jsonl")}


def _raw(path: Path, item: dict, manifest: dict) -> tuple[dict, dict, int]:
    raw = first._read(path / (item["id"] + ".response.json"))
    if (raw.get("finish_reason") != "FINISH_REASON_STOP" or raw.get("tool_calls")
            or raw.get("model", {}).get("model_profile_id") != manifest["profile"]
            or raw.get("model", {}).get("tier") != manifest["tier"]):
        raise sj.JudgeError("original response is not a complete result from the frozen judge")
    result = raw["result"] if "result" in raw else json.loads(raw["text"])
    cost = int(raw["usage"]["cost_usd_micros"])
    if cost < 0:
        raise sj.JudgeError("original response has invalid accounting")
    return raw, result, cost


def _parent(path: Path, proof: dict) -> tuple[dict, dict, dict, dict, list[dict]]:
    manifest, plan, export = (first._read(path / name) for name in ("manifest.json", "plan.json", "export.json"))
    receipt = first._read(path / "recovery.json")
    if (receipt.get("kind") != formatted.KIND or manifest.get("schema_transport_version") != 2
            or manifest.get("legacy_completed_item_ids") != []
            or pv.digest(plan) != manifest.get("plan_sha256") or pv.digest(export) != manifest.get("export_sha256")
            or sj._request_digests(manifest, export) != manifest.get("request_sha256")):
        raise sj.JudgeError("raw extraction requires the unchanged uniform strict cohort")
    prior = formatted.validate(path, manifest, receipt)
    items = sorted(export["items"], key=lambda item: item["id"])
    calls = first._journal(path / "calls.jsonl")
    if (len(calls) != 10 or items[4]["id"] != ITEM
            or [row.get("state") for row in calls] != ["pending", "completed"] * 4 + ["pending", "failed"]
            or [row.get("id") for row in calls] != [item["id"] for item in items[:5] for _ in range(2)]
            or calls[-1].get("error_type") != "JudgeError" or (path / "review.json").exists()
            or {p.name for p in path.glob("*.response.json")} != {item["id"] + ".response.json" for item in items[:5]}):
        raise sj.JudgeError("only the reviewed fifth local-schema failure may be extracted")
    records = []
    for index, item in enumerate(items[:5]):
        raw, result, cost = _raw(path, item, manifest)
        if index < 4:
            record = calls[index * 2 + 1]
            if (record.get("review") != sj.validate_scores(item, result) or record.get("rpc_cost_micros") != cost
                    or record.get("response_sha256") != pv.digest(raw)
                    or record.get("node_id") != f"eval_call_{index + 5:08d}"):
                raise sj.JudgeError("an already completed judgment changed")
        else:
            try:
                sj.validate_scores(item, result)
            except sj.JudgeError:
                pass
            else:
                raise sj.JudgeError("the original raw-schema failure must remain visible")
            review, metadata = sj.extract_scores(item, result)
            if cost != COST or metadata != {"raw_schema_valid": False, "result_extraction_version": EXTRACTION,
                                            "ignored_root_fields": ["overall_assessment"]}:
                raise sj.JudgeError("only the reviewed unscored root string may be ignored")
            record = {"id": ITEM, "state": "completed", "node_id": "eval_call_00000009",
                      "rpc_cost_micros": COST, "review": review, "response_sha256": pv.digest(raw), **metadata}
        records.append(record)
    if sum(row["rpc_cost_micros"] for row in records) + formatted.KNOWN_TOTAL != KNOWN_TOTAL:
        raise sj.JudgeError("original paid results cannot be refunded or changed")
    if first._read(path / "budget.json") != {"limit_usd_micros": first.CAP_MICROS - first.HELD_MICROS,
                                               "spent_usd_micros": KNOWN_TOTAL, "pending": False}:
        raise sj.JudgeError("all five returned calls must already be fully charged")
    identity = first._read(path / "usage/identity.json")
    if (identity.get("run_id") != manifest["run_id"] or identity.get("task_id") != manifest["task_id"]
            or identity.get("tenant_id") != first.TENANT or identity.get("gateway_address") != manifest["gateway_address"]
            or identity.get("sequence_offset") != 4):
        raise sj.JudgeError("original audit identity changed")
    audit = first._journal(path / "usage/calls.jsonl")
    if (len(audit) != 10 or [row.get("sequence") for row in audit] != [i for i in range(5, 10) for _ in range(2)]
            or [row.get("state") for row in audit] != ["pending", "completed"] * 5):
        raise sj.JudgeError("every original RPC must have a complete audit record")
    for record, item, pair in zip(records, items[:5], zip(audit[::2], audit[1::2])):
        if (any(row.get("task_id") != manifest["task_id"] or row.get("node_id") != record["node_id"]
                or row.get("source_node_id") != "semantic_" + item["id"] for row in pair)
                or pair[1].get("known_cost_usd_micros") != record["rpc_cost_micros"]):
            raise sj.JudgeError("original RPC accounting differs from the saved response")
    rows = proof.get("attempts", proof.get("rows", []))
    if (proof.get("tenant_id") != first.TENANT or proof.get("task_id", manifest["task_id"]) != manifest["task_id"]
            or len(rows) != 9 or len({row["reservation_id"] for row in rows}) != 9):
        raise sj.JudgeError("raw recovery requires all nine original ledger attempts")
    target = next((row for row in rows if row["reservation_id"] == RESERVATION), None)
    if (target is None or target.get("node_id") != "eval_call_00000009"
            or target.get("actual_usd_micros") != COST or target.get("input_tokens") != 10757
            or target.get("output_tokens") != 942):
        raise sj.JudgeError("the reviewed fifth reservation changed")
    sj._ledger({"tenant_id": first.TENANT, "task_id": manifest["task_id"], "attempts": rows}, manifest,
               {row["id"]: row for row in records}, KNOWN_TOTAL, prior["retained_pending"], prior["retained_prior_known"])
    return manifest, plan, export, prior, records


def _estimate(manifest: dict, export: dict) -> Decimal:
    remaining = {**export, "items": sorted(export["items"], key=lambda item: item["id"])[5:]}
    return formatted._estimate(manifest, remaining)


def prepare(parent: Path, out: Path, proof: dict) -> dict:
    parent, out = Path(parent).resolve(), Path(out).resolve()
    if parent == out or out.exists():
        raise sj.JudgeError("raw extraction requires a separate new directory")
    manifest, plan, export, prior, records = _parent(parent, proof)
    estimate = _estimate(manifest, export)
    if estimate + Decimal(KNOWN_TOTAL + first.HELD_MICROS) / 1_000_000 > 7:
        raise sj.JudgeError("remaining unchanged items and prior charges exceed the original allowance")
    copied = sj.prepare(plan, export, out, tenant_id=manifest["tenant_id"], gateway_address=manifest["gateway_address"],
                        tier=manifest["tier"], max_output_tokens=12000, thinking_budget_tokens=8192,
                        schema_transport_version=2)
    if copied != manifest:
        raise sj.JudgeError("raw extraction changed the original requests")
    for row in records:
        shutil.copy2(parent / (row["id"] + ".response.json"), out / (row["id"] + ".response.json"))
        sj._append(out / "calls.jsonl", row)
    snapshot = {"tenant_id": first.TENANT, "task_id": manifest["task_id"],
                "attempts": proof.get("attempts", proof.get("rows", []))}
    sj._write(out / "parent-ledger.json", snapshot)
    receipt = {"kind": KIND, "original_directory": str(parent), "parent_files": _files(parent),
               "preserved_directories": [str(parent), *prior["preserved_directories"]],
               "manifest_sha256": pv.digest(manifest), "parent_ledger_sha256": first._hash(out / "parent-ledger.json"),
               "retained_pending": prior["retained_pending"], "retained_prior_known": prior["retained_prior_known"],
               "seed_records": records, "sequence_offset": 9, "original_cap_usd_micros": first.CAP_MICROS,
               "held_usd_micros": first.HELD_MICROS, "execution_cap_usd_micros": first.CAP_MICROS - first.HELD_MICROS,
               "prior_known_usd_micros": formatted.KNOWN_TOTAL, "original_spent_usd_micros": KNOWN_TOTAL,
               "result_extraction_version": EXTRACTION, "derived_item_id": ITEM, "new_model_calls_for_recovery": 0,
               "remaining_items": 17, "remaining_estimate_usd": str(estimate), "actual_total_cost_known": False}
    sj._write(out / "recovery.json", receipt)
    return receipt


def validate(out: Path, manifest: dict, receipt: dict) -> dict:
    out, parent = Path(out).resolve(), Path(receipt["original_directory"]).resolve()
    if (out == parent or receipt.get("kind") != KIND or _files(parent) != receipt.get("parent_files")
            or first._hash(out / "parent-ledger.json") != receipt.get("parent_ledger_sha256")):
        raise sj.JudgeError("original raw extraction evidence changed")
    previous, _, export, prior, records = _parent(parent, first._read(out / "parent-ledger.json"))
    fixed = {"sequence_offset": 9, "original_cap_usd_micros": first.CAP_MICROS, "held_usd_micros": first.HELD_MICROS,
             "execution_cap_usd_micros": first.CAP_MICROS - first.HELD_MICROS,
             "prior_known_usd_micros": formatted.KNOWN_TOTAL, "original_spent_usd_micros": KNOWN_TOTAL,
             "remaining_items": 17, "new_model_calls_for_recovery": 0}
    if (previous != manifest or receipt.get("manifest_sha256") != pv.digest(manifest)
            or any(type(receipt.get(key)) is not int or receipt[key] != value for key, value in fixed.items())
            or receipt.get("retained_pending") != prior["retained_pending"]
            or receipt.get("retained_prior_known") != prior["retained_prior_known"]
            or receipt.get("preserved_directories") != [str(parent), *prior["preserved_directories"]]
            or receipt.get("seed_records") != records or receipt.get("result_extraction_version") != EXTRACTION
            or receipt.get("derived_item_id") != ITEM or receipt.get("actual_total_cost_known") is not False):
        raise sj.JudgeError("raw extraction changed grading conditions or prior accounting")
    if first._journal(out / "calls.jsonl")[:5] != records:
        raise sj.JudgeError("the five retained judgments changed")
    for record in records:
        name = record["id"] + ".response.json"
        if (out / name).read_bytes() != (parent / name).read_bytes():
            raise sj.JudgeError("retained raw response bytes changed")
    estimate = Decimal(receipt["remaining_estimate_usd"])
    if (not estimate.is_finite() or estimate != _estimate(manifest, export)
            or estimate + Decimal(KNOWN_TOTAL + first.HELD_MICROS) / 1_000_000 > 7):
        raise sj.JudgeError("raw extraction exceeds the original allowance")
    return receipt
