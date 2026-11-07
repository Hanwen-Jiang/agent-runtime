"""Retain two completed judgments and one billed, invalid-JSON attempt.

Only the reviewed seq4 failure is admitted. Previous judgments retain their
original bytes separately; the complete 22-item comparison uses one strict schema
while the local rubric validator is unchanged. No database settlement occurs.
"""
from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path

from agentruntime_learning.benchmarks import paired_semantic_judge as sj, paired_semantic_recovery as first
from agentruntime_learning.benchmarks import provenance as pv

KIND = "paired-semantic-paid-format-v1"
BAD_ID = "2b0688ac4e72aa9f1ced387b"
BAD_RID = "mbr_D4BBPR3QQFCYTLTJASM47SBBEW"
BAD_COST = 47_706
KNOWN_TOTAL = 116_987


def _estimate(manifest: dict, export: dict) -> Decimal:
    total = Decimal(0)
    for item in export["items"]:
        req = sj._request(manifest, export, item)
        size = sum(len(message.text.encode()) for message in req.generation.messages) + len(req.generation.output_schema.SerializeToString()) + 4096
        total += Decimal(size * 4 + 12000 * 20) / 1_000_000
    return total


def _parent(path: Path, proof: dict | None = None) -> tuple[dict, dict, dict, dict, list[dict]]:
    manifest, plan, export = (first._read(path / name) for name in ("manifest.json", "plan.json", "export.json"))
    if pv.digest(plan) != manifest.get("plan_sha256") or pv.digest(export) != manifest.get("export_sha256"):
        raise sj.JudgeError("parent scoring inputs or price assumptions changed")
    if first._read(path / "recovery.json").get("kind") != first.KIND:
        raise sj.JudgeError("format recovery cannot be chained or repeated")
    if manifest.get("schema_transport_version", 1) != 1:
        raise sj.JudgeError("this recovery is only for the observed legacy schema failure")
    prior = first.validate(path, manifest)
    if prior is None or prior["kind"] != first.KIND:
        raise sj.JudgeError("format recovery requires the original reviewed first-call recovery")
    calls = first._journal(path / "calls.jsonl")
    latest = {row["id"]: row for row in calls}
    completed = [row for row in latest.values() if row["state"] == "completed"]
    failed = [row for row in latest.values() if row["state"] != "completed"]
    if (len(calls) != 6 or len(completed) != 2 or len(failed) != 1
            or failed[0].get("id") != BAD_ID or failed[0].get("state") != "failed"
            or failed[0].get("error_type") != "InvalidError"
            or [row["id"] for row in calls[::2]] != sorted(manifest["item_ids"])[:3]
            or [row["state"] for row in calls] != ["pending", "completed", "pending", "completed", "pending", "failed"]):
        raise sj.JudgeError("only the reviewed unscored format failure may be resumed")
    items = {item["id"]: item for item in export["items"]}
    if {p.name for p in path.glob("*.response.json")} != {row["id"] + ".response.json" for row in completed}:
        raise sj.JudgeError("original response evidence changed")
    for row in completed:
        raw = first._read(path / (row["id"] + ".response.json"))
        data = raw["result"] if "result" in raw else json.loads(raw["text"])
        if (pv.digest(raw) != row["response_sha256"] or sj.validate_scores(items[row["id"]], data) != row["review"]
                or int(raw["usage"]["cost_usd_micros"]) != row["rpc_cost_micros"]
                or raw["model"]["model_profile_id"] != manifest["profile"]
                or raw["model"]["tier"] != manifest["tier"] or raw.get("finish_reason") != "FINISH_REASON_STOP"):
            raise sj.JudgeError("valid original judgment bytes or costs changed")
    if sum(row["rpc_cost_micros"] for row in completed) + BAD_COST != KNOWN_TOTAL:
        raise sj.JudgeError("original known payments changed")
    budget = first._read(path / "budget.json")
    if budget != {"limit_usd_micros": first.CAP_MICROS - first.HELD_MICROS,
                  "spent_usd_micros": KNOWN_TOTAL, "pending": True}:
        raise sj.JudgeError("the charged format failure cannot be refunded or reset")
    audit = first._journal(path / "usage/calls.jsonl")
    if (len(audit) != 6 or [row.get("sequence") for row in audit] != [2, 2, 3, 3, 4, 4]
            or [row.get("state") for row in audit] != ["pending", "completed", "pending", "completed", "pending", "unresolved"]
            or audit[-1].get("known_cost_usd_micros") != BAD_COST or audit[-1].get("error_type") != "InvalidError"
            or audit[-1].get("source_node_id") != "semantic_" + BAD_ID):
        raise sj.JudgeError("the paid failure lacks its original audit evidence")
    if proof is not None:
        rows = proof.get("attempts", proof.get("rows", []))
        if proof.get("tenant_id") != first.TENANT or len(rows) != 4 or len({r["reservation_id"] for r in rows}) != 4:
            raise sj.JudgeError("all four original attempts must be retained")
        old = {row["reservation_id"]: row for row in prior["retained_pending"]}
        if {r["reservation_id"]: r for r in rows if r["reservation_id"] in old} != old:
            raise sj.JudgeError("original unknown payment changed")
        records = {row["id"]: row for row in completed}
        bad = next((row for row in rows if row["reservation_id"] == BAD_RID), None)
        _known_failure(bad, manifest)
        sj._ledger({"tenant_id": first.TENANT, "task_id": manifest["task_id"], "attempts": rows},
                   manifest, records, KNOWN_TOTAL, prior["retained_pending"], [bad])
    return manifest, plan, export, prior, completed


def _known_failure(row: dict | None, manifest: dict) -> None:
    if (not isinstance(row, dict) or row.get("reservation_id") != BAD_RID
            or row.get("node_id") != "eval_call_00000004" or row.get("task_id") != manifest["task_id"]
            or row.get("model_profile_id") != first.PROFILE or row.get("method") != "generate"
            or type(row.get("actual_usd_micros")) is not int or row["actual_usd_micros"] != BAD_COST
            or row.get("input_tokens") != 9457 or row.get("output_tokens") != 739
            or row.get("registry_scope") != manifest["registry"]["scope"]
            or row.get("registry_revision") != manifest["registry"]["revision"]
            or row.get("registry_document_sha256") != manifest["registry"]["document_sha256"]):
        raise sj.JudgeError("the reviewed format failure charge changed")


def prepare(parent: Path, out: Path, proof: dict) -> dict:
    parent, out = Path(parent).resolve(), Path(out).resolve()
    if parent == out or out.exists():
        raise sj.JudgeError("format recovery requires a new descendant directory")
    previous, plan, export, prior, completed = _parent(parent, proof)
    ids = sorted(row["id"] for row in completed)
    manifest = sj.prepare(plan, export, out, tenant_id=previous["tenant_id"], gateway_address=previous["gateway_address"],
                          tier=previous["tier"], max_output_tokens=12000, thinking_budget_tokens=8192,
                          schema_transport_version=2)
    archive = out / "prior-valid-responses"
    archive.mkdir()
    for row in completed:
        shutil.copy2(parent / (row["id"] + ".response.json"), archive / (row["id"] + ".response.json"))
    sj._write(out / "prior-valid-judgments.json", completed)
    rows = proof.get("attempts", proof.get("rows", []))
    bad = next(row for row in rows if row["reservation_id"] == BAD_RID)
    remaining = list(export["items"])
    estimate = _estimate(manifest, export)
    if estimate + Decimal(KNOWN_TOTAL + first.HELD_MICROS) / 1_000_000 > 7:
        raise sj.JudgeError("remaining unchanged items and all prior costs exceed the original allowance")
    names = (*first.FILES, "recovery.json", *(identity + ".response.json" for identity in ids))
    receipt = {"kind": KIND, "original_directory": str(parent), "parent_files": {name: first._hash(parent / name) for name in names},
               "preserved_directories": [str(parent), prior["original_directory"]], "manifest_sha256": pv.digest(manifest),
               "retained_pending": prior["retained_pending"], "retained_prior_known": [row for row in rows if row["actual_usd_micros"] is not None],
               "sequence_offset": 4, "original_cap_usd_micros": first.CAP_MICROS, "held_usd_micros": first.HELD_MICROS,
               "execution_cap_usd_micros": first.CAP_MICROS - first.HELD_MICROS, "prior_known_usd_micros": KNOWN_TOTAL,
               "prior_valid_item_ids": ids, "retry_item_id": BAD_ID, "format_retry_limit": 1, "uniform_new_schema": True,
               "remaining_items": len(remaining), "remaining_estimate_usd": str(estimate), "actual_total_cost_known": False}
    sj._write(out / "recovery.json", receipt)
    return receipt


def validate(out: Path, manifest: dict, receipt: dict) -> dict:
    parent = Path(receipt["original_directory"])
    previous, plan, export, prior, completed = _parent(parent)
    ids = sorted(row["id"] for row in completed)
    names = {*first.FILES, "recovery.json", *(identity + ".response.json" for identity in ids)}
    if (parent.resolve() == Path(out).resolve() or set(receipt.get("parent_files", {})) != names
            or any(first._hash(parent / name) != value for name, value in receipt["parent_files"].items())
            or receipt.get("manifest_sha256") != pv.digest(manifest) or manifest.get("schema_transport_version") != 2
            or manifest.get("legacy_completed_item_ids") != [] or receipt.get("prior_valid_item_ids") != ids or receipt.get("uniform_new_schema") is not True
            or receipt.get("sequence_offset") != 4 or receipt.get("held_usd_micros") != first.HELD_MICROS
            or receipt.get("original_cap_usd_micros") != first.CAP_MICROS
            or receipt.get("execution_cap_usd_micros") != first.CAP_MICROS - first.HELD_MICROS
            or receipt.get("prior_known_usd_micros") != KNOWN_TOTAL or receipt.get("format_retry_limit") != 1
            or receipt.get("retry_item_id") != BAD_ID or receipt.get("actual_total_cost_known") is not False
            or receipt.get("preserved_directories") != [str(parent.resolve()), prior["original_directory"]]
            or receipt.get("retained_pending") != prior["retained_pending"]):
        raise sj.JudgeError("format recovery changed its original evidence or allowance")
    if any(type(receipt.get(key)) is not int for key in ("sequence_offset", "held_usd_micros", "original_cap_usd_micros",
            "execution_cap_usd_micros", "prior_known_usd_micros", "format_retry_limit", "remaining_items")):
        raise sj.JudgeError("format recovery limits must be exact integers")
    for key in ("run_id", "task_id", "plan_sha256", "export_sha256", "tenant_id", "gateway_address", "profile", "registry",
                "tier", "max_output_tokens", "thinking_budget_tokens", "system_prompt", "material_version", "budget_usd", "item_ids"):
        if manifest.get(key) != previous.get(key):
            raise sj.JudgeError("format recovery changed grading conditions")
    for row in completed:
        if (parent / (row["id"] + ".response.json")).read_bytes() != (Path(out) / "prior-valid-responses" / (row["id"] + ".response.json")).read_bytes():
            raise sj.JudgeError("previous valid response bytes changed")
    if first._read(Path(out) / "prior-valid-judgments.json") != completed:
        raise sj.JudgeError("previous valid judgments changed")
    known = receipt.get("retained_prior_known", [])
    if len(known) != 3 or len({row["reservation_id"] for row in known}) != 3:
        raise sj.JudgeError("all previous settled attempts must remain accounted")
    bad = next((row for row in known if row["reservation_id"] == BAD_RID), None)
    _known_failure(bad, manifest)
    records = {row["id"]: row for row in completed}
    sj._ledger({"tenant_id": first.TENANT, "task_id": manifest["task_id"],
                "attempts": [*receipt["retained_pending"], *known]}, previous,
               records, KNOWN_TOTAL, receipt["retained_pending"], [bad])
    estimate = Decimal(receipt["remaining_estimate_usd"])
    if (not estimate.is_finite() or estimate != _estimate(manifest, export)
            or estimate + Decimal(KNOWN_TOTAL + first.HELD_MICROS) / 1_000_000 > 7 or receipt.get("remaining_items") != 22):
        raise sj.JudgeError("format recovery exceeds the unchanged allowance")
    return receipt
