"""One explicitly reviewed recovery of the first, unreturned semantic RPC.

The original directory stays byte-identical. A descendant directory uses the
same run/Task/item identities, starts after audit sequence one, and reserves the
unknown charge inside the original seven-dollar allowance. No bill is settled.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from agentruntime_learning.benchmarks import paired_semantic_judge as sj, provenance as pv
from agentruntime_learning.evals.audited_gateway import task_identity

KIND = "paired-semantic-first-unreturned-v1"
RUN = "n16-evreal-pinned-20261107"
TENANT = "gh-goalpairjudge-20261107"
PROFILE = "a6api/claude-opus-5-5@2026-10"
PENDING = "mbr_WO3FHOU4DZAELVWGLO3BNQOPXX"
HELD_MICROS = 298_752
CAP_MICROS = 7_000_000
FILES = ("manifest.json", "plan.json", "export.json", "calls.jsonl", "budget.json",
         "usage/identity.json", "usage/calls.jsonl")


def _read(path: Path) -> Any:
    return json.loads(path.read_text())


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _journal(path: Path) -> list[dict]:
    data = path.read_bytes()
    if not data.endswith(b"\n"):
        raise sj.JudgeError("incomplete original journal")
    return [json.loads(line) for line in data.splitlines() if line.strip()]


def _original(original: Path) -> tuple[dict, dict, dict]:
    manifest, plan, export = (_read(original / name) for name in ("manifest.json", "plan.json", "export.json"))
    profile, registry, tokens = sj._contract(plan, export)
    if (plan["run_id"] != RUN or manifest["run_id"] != RUN + "-semantic-judge"
            or manifest["task_id"] != task_identity(manifest["run_id"])
            or manifest["tenant_id"] != TENANT or profile != PROFILE or manifest["profile"] != PROFILE
            or manifest["registry"] != registry or manifest["tier"] != "strong@medium"
            or manifest["max_output_tokens"] != 12000 or manifest["thinking_budget_tokens"] != 8192
            or tokens != 12000 or manifest["budget_usd"] != 7
            or manifest["plan_sha256"] != pv.digest(plan) or manifest["export_sha256"] != pv.digest(export)
            or manifest["system_prompt"] != sj.SYSTEM or manifest["material_version"] != sj.MATERIAL_VERSION
            or manifest["request_sha256"] != sj._request_digests(manifest, export)):
        raise sj.JudgeError("original fixed judgment contract changed")
    first = min(manifest["item_ids"])
    calls = _journal(original / "calls.jsonl")
    if (len(calls) != 2 or [row.get("state") for row in calls] != ["pending", "failed"]
            or any(row.get("id") != first for row in calls) or calls[-1].get("error_type") != "ConflictError"
            or list(original.glob("*.response.json")) or (original / "review.json").exists()):
        raise sj.JudgeError("only the first unreturned item may be retried once")
    budget = _read(original / "budget.json")
    if (budget != {"limit_usd_micros": CAP_MICROS, "spent_usd_micros": 0, "pending": True}
            or type(budget.get("limit_usd_micros")) is not int or type(budget.get("spent_usd_micros")) is not int
            or budget.get("pending") is not True):
        raise sj.JudgeError("original unresolved budget must remain unchanged")
    identity = _read(original / "usage/identity.json")
    if (identity.get("task_id") != manifest["task_id"] or identity.get("tenant_id") != TENANT
            or identity.get("run_id") != manifest["run_id"]
            or identity.get("gateway_address") != manifest["gateway_address"] or identity.get("sequence_offset", 0) != 0):
        raise sj.JudgeError("original audit identity changed")
    audit = _journal(original / "usage/calls.jsonl")
    if (len(audit) != 2 or [row.get("state") for row in audit] != ["pending", "unresolved"]
            or any(row.get("sequence") != 1 or row.get("node_id") != "eval_call_00000001" for row in audit)
            or audit[-1].get("known_cost_usd_micros") is not None):
        raise sj.JudgeError("original unreturned attempt is not unique")
    item = next(item for item in export["items"] if item["id"] == first)
    request = sj._request(manifest, export, item)
    request.tenant.tenant_id = TENANT
    request.call.node_id = "eval_call_00000001"
    request.call.max_cost_usd_micros = CAP_MICROS
    actual_hash = hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()
    if any(row.get("request_sha256") != actual_hash or row.get("task_id") != manifest["task_id"]
           or row.get("source_task_id") != manifest["task_id"] or row.get("source_node_id") != "semantic_" + first
           or row.get("method") != "generate" or row.get("pinned_model_profile") != PROFILE
           or row.get("max_cost_usd_micros") != CAP_MICROS or type(row.get("sequence")) is not int for row in audit):
        raise sj.JudgeError("original wire request differs from the fixed material")
    return manifest, plan, export


def _pending(row: dict, manifest: dict) -> None:
    expected = {"reservation_id": PENDING, "task_id": manifest["task_id"], "node_id": "eval_call_00000001",
                "method": "generate", "model_profile_id": PROFILE, "actual_usd_micros": None,
                "reserved_usd_micros": HELD_MICROS, "input_tokens": None, "output_tokens": None,
                "registry_scope": manifest["registry"]["scope"], "registry_revision": manifest["registry"]["revision"],
                "registry_document_sha256": manifest["registry"]["document_sha256"]}
    if (any(key not in row or row[key] != value for key, value in expected.items())
            or type(row.get("reserved_at")) is not int or row["reserved_at"] <= 0
            or type(row.get("reserved_usd_micros")) is not int):
        raise sj.JudgeError("only the reviewed first pending reservation may be retained")


def prepare(original: Path, out: Path, proof: dict, estimate_path: Path) -> dict:
    original, out = Path(original).resolve(), Path(out).resolve()
    if original == out or out.exists():
        raise sj.JudgeError("recovery requires a separate, new directory")
    manifest, plan, export = _original(original)
    rows = proof.get("attempts", proof.get("rows", []))
    if proof.get("tenant_id") != TENANT or len(rows) != 1:
        raise sj.JudgeError("recovery requires the complete isolated judge ledger")
    _pending(rows[0], manifest)
    estimate = _read(estimate_path)
    projected = Decimal(str(estimate["estimated_usd"]))
    if (not projected.is_finite() or projected < 0 or projected + Decimal(HELD_MICROS) / 1_000_000 > 7
            or estimate["request_sha256"] != manifest["request_sha256"]):
        raise sj.JudgeError("full unchanged cohort plus retained charge exceeds the original allowance")
    prepared = sj.prepare(plan, export, out, tenant_id=TENANT, gateway_address=manifest["gateway_address"],
                          tier=manifest["tier"], max_output_tokens=12000, thinking_budget_tokens=8192)
    if prepared != manifest:
        raise sj.JudgeError("recovery changed original requests")
    receipt = {"kind": KIND, "original_directory": str(original), "original_files": {name: _hash(original / name) for name in FILES},
               "manifest_sha256": pv.digest(manifest), "estimate_path": str(Path(estimate_path).resolve()),
               "estimate_sha256": _hash(estimate_path), "retained_pending": rows, "retained_pending_sha256": pv.digest(rows),
               "sequence_offset": 1, "original_cap_usd_micros": CAP_MICROS,
               "held_usd_micros": HELD_MICROS, "execution_cap_usd_micros": CAP_MICROS - HELD_MICROS,
               "actual_total_cost_known": False, "first_item_retry_limit": 1}
    sj._write(out / "recovery.json", receipt)
    return receipt


def validate(out: Path, manifest: dict) -> dict | None:
    path = Path(out) / "recovery.json"
    if not path.exists():
        return None
    receipt = _read(path)
    if (any(type(receipt.get(key)) is not int for key in ("sequence_offset", "original_cap_usd_micros", "held_usd_micros", "execution_cap_usd_micros", "first_item_retry_limit"))
            or receipt.get("kind") != KIND or receipt.get("sequence_offset") != 1
            or receipt.get("original_cap_usd_micros") != CAP_MICROS or receipt.get("held_usd_micros") != HELD_MICROS
            or receipt.get("execution_cap_usd_micros") != CAP_MICROS - HELD_MICROS
            or receipt.get("first_item_retry_limit") != 1 or receipt.get("actual_total_cost_known") is not False):
        raise sj.JudgeError("reviewed recovery limits changed")
    original = Path(receipt["original_directory"])
    if original.resolve() == Path(out).resolve() or set(receipt["original_files"]) != set(FILES):
        raise sj.JudgeError("original receipt identity changed")
    if any(_hash(original / name) != value for name, value in receipt["original_files"].items()):
        raise sj.JudgeError("original paid evidence changed")
    previous, _, _ = _original(original)
    if previous != manifest or receipt["manifest_sha256"] != pv.digest(manifest):
        raise sj.JudgeError("recovery mixes different material or model settings")
    estimate_path = Path(receipt["estimate_path"])
    if _hash(estimate_path) != receipt["estimate_sha256"]:
        raise sj.JudgeError("original cost estimate changed")
    estimate = _read(estimate_path)
    projected = Decimal(str(estimate["estimated_usd"]))
    if (not projected.is_finite() or projected < 0 or estimate["request_sha256"] != manifest["request_sha256"]
            or projected + Decimal(HELD_MICROS) / 1_000_000 > 7):
        raise sj.JudgeError("original shared allowance is not sufficient")
    pending = receipt["retained_pending"]
    if len(pending) != 1 or pv.digest(pending) != receipt["retained_pending_sha256"]:
        raise sj.JudgeError("retained pending evidence changed")
    _pending(pending[0], manifest)
    return receipt
