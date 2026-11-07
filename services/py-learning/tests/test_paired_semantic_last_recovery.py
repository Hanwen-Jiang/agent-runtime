"""Only the final unreturned judgment may run once; the other 21 are reused."""
from __future__ import annotations

import copy
import hashlib
import json

from google.protobuf import json_format
import pytest

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_learning.benchmarks import paired_semantic_judge as sj, paired_semantic_recovery as first
from agentruntime_learning.benchmarks import paired_semantic_raw_recovery as extracted
from agentruntime_learning.benchmarks import paired_semantic_last_recovery as recovery, provenance as pv
from test_paired_semantic_raw_recovery import raw_fixture
from test_paired_semantic_format_recovery import preserved_bytes
from test_paired_semantic_recovery import write, journal
from test_paired_semantic_judge import FakeGateway, judgment


def last_fixture(tmp_path):
    plan, export, parents, parent, manifest, proof = raw_fixture(tmp_path)
    extracted.prepare(parents[-1], parent, proof)
    calls = first._journal(parent / "calls.jsonl")
    rows, audit, spent = copy.deepcopy(proof["attempts"]), [], 332277
    costs = [39000] * 15 + [49841, 35290]
    assert export["items"][-1]["id"] == recovery.ITEM
    for sequence, item, cost in zip(range(10, 27), export["items"][5:], costs):
        node = f"eval_call_{sequence:08d}"
        calls.append({"id": item["id"], "state": "pending", "evidence_sha256": item["evidence_sha256"]})
        request = sj._request(manifest, export, item)
        request.tenant.tenant_id = first.TENANT
        request.call.node_id = node
        request.call.max_cost_usd_micros = 6701248 - spent
        entry = {"sequence": sequence, "state": "pending", "method": "generate", "task_id": manifest["task_id"],
            "node_id": node, "source_node_id": "semantic_" + item["id"], "source_task_id": manifest["task_id"],
            "request_sha256": hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest(),
            "max_cost_usd_micros": request.call.max_cost_usd_micros, "role": request.call.role,
            "pinned_model_profile": first.PROFILE}
        audit.append(entry)
        rows.append({**rows[0], "reservation_id": recovery.RESERVATION if sequence == 26 else f"extracted-{sequence}",
            "node_id": node, "actual_usd_micros": cost, "reserved_usd_micros": 344176,
            "reserved_at": 1794093000000 + sequence, "input_tokens": 2136, "output_tokens": 1354})
        if sequence == 26:
            calls.append({"id": item["id"], "state": "failed", "error_type": "InvalidError"})
            audit.append({**entry, "state": "unresolved", "known_cost_usd_micros": cost, "error_type": "InvalidError"})
        else:
            result = judgment(item)
            for score in result["scores"].values():
                score["score"] = .5
            response = mg.GenerateResponse(text=json.dumps(result), finish_reason=mg.FINISH_REASON_STOP)
            response.model.model_profile_id = first.PROFILE
            response.model.tier = "strong@medium"
            response.usage.cost_usd_micros = cost
            response.usage.input_tokens = 2136
            response.usage.output_tokens = 1354
            raw = json_format.MessageToDict(response, preserving_proto_field_name=True)
            write(parent / (item["id"] + ".response.json"), raw)
            review, metadata = sj.extract_scores(item, result)
            calls.append({"id": item["id"], "state": "completed", "node_id": node, "rpc_cost_micros": cost,
                "review": review, "response_sha256": pv.digest(raw), **metadata})
            audit.append({**entry, "state": "completed", "known_cost_usd_micros": cost,
                "served_model_profile": first.PROFILE, "input_tokens": 2136, "output_tokens": 1354})
        spent += cost
    assert spent == 1002408
    journal(parent / "calls.jsonl", calls)
    journal(parent / "usage/calls.jsonl", audit)
    write(parent / "usage/identity.json", {"run_id": manifest["run_id"], "task_id": manifest["task_id"],
        "tenant_id": first.TENANT, "source_tenant_id": "evals", "gateway_address": manifest["gateway_address"], "sequence_offset": 9})
    write(parent / "budget.json", {"limit_usd_micros": 6701248, "spent_usd_micros": spent, "pending": True})
    proof["attempts"] = rows
    return plan, export, (*parents, parent), tmp_path / "last", manifest, proof


class LastGateway(FakeGateway):
    def __init__(self, plan, export, rows):
        super().__init__(plan, export, served=first.PROFILE)
        self.tenant = first.TENANT
        self.identity["sequence_offset"] = self.sequence = 26
        self.items = [None] * 26 + self.items[-1:]
        self.rows = copy.deepcopy(rows)

    async def generate(self, request):
        response = await super().generate(request)
        self.rows[-1].update(registry_revision="v3",
            registry_document_sha256="e42df3b1eba85d89bcf869ef8942c993d469e4ca3f03818789dc1cea1bed0d5a")
        return response


def test_prepare_retains_21_scores_and_all_original_payments_without_calls(tmp_path):
    _, _, parents, out, manifest, proof = last_fixture(tmp_path)
    before = preserved_bytes(*parents)
    receipt = recovery.prepare(parents[-1], out, proof)
    assert first.validate(out, manifest) == receipt
    assert json.loads((out / "manifest.json").read_text()) == manifest
    records = sj._history(out)
    assert len(records) == 21 and recovery.ITEM not in records
    assert sum(row["rpc_cost_micros"] for row in records.values()) == 850131
    assert sum(row["actual_usd_micros"] for row in receipt["retained_prior_known"]) == 152277
    assert receipt["sequence_offset"] == 26 and receipt["remaining_items"] == 1
    assert receipt["execution_cap_usd_micros"] - receipt["original_spent_usd_micros"] == 5698840
    assert receipt["retained_pending"] == proof["attempts"][:1]
    assert len(list(out.glob("*.response.json"))) == 21
    assert preserved_bytes(*parents) == before and not (out / "budget.json").exists()
    with pytest.raises(sj.JudgeError):
        recovery.prepare(out, tmp_path / "last-again", proof)


@pytest.mark.parametrize("change", ["already_returned", "missing_row", "changed_old_row", "last_cost", "last_id", "budget", "configuration"])
def test_prepare_rejects_new_authority_or_changed_prior_evidence(tmp_path, change):
    _, _, parents, out, _, proof = last_fixture(tmp_path)
    parent = parents[-1]
    if change == "already_returned":
        write(parent / (recovery.ITEM + ".response.json"), {"text": "already returned"})
    elif change == "missing_row":
        proof["attempts"].pop(1)
    elif change == "changed_old_row":
        proof["attempts"][5]["reserved_at"] += 1
    elif change == "last_cost":
        proof["attempts"][-1]["actual_usd_micros"] -= 1
    elif change == "last_id":
        proof["attempts"][-1]["reservation_id"] = "unreviewed"
    elif change == "budget":
        write(parent / "budget.json", {"limit_usd_micros": 6701248, "spent_usd_micros": 967118, "pending": True})
    else:
        manifest = json.loads((parent / "manifest.json").read_text())
        manifest["schema_transport_version"] = 1
        write(parent / "manifest.json", manifest)
    with pytest.raises(sj.JudgeError):
        recovery.prepare(parent, out, proof)
    assert not out.exists()


@pytest.mark.parametrize("change", ["parent", "raw", "seed", "snapshot", "refund", "retry_limit", "estimate"])
def test_validate_rejects_modification_of_parent_seed_accounting_or_retry_limit(tmp_path, change):
    _, export, parents, out, manifest, proof = last_fixture(tmp_path)
    receipt = recovery.prepare(parents[-1], out, proof)
    if change in ("parent", "raw"):
        path = parents[-1] / "calls.jsonl" if change == "parent" else out / (export["items"][0]["id"] + ".response.json")
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "seed":
        records = first._journal(out / "calls.jsonl")
        records.pop(0)
        journal(out / "calls.jsonl", records)
    elif change == "snapshot":
        ledger = json.loads((out / "parent-ledger.json").read_text())
        ledger["attempts"][-1]["reserved_at"] += 1
        write(out / "parent-ledger.json", ledger)
    else:
        key, value = {"refund": ("prior_known_usd_micros", 116987), "retry_limit": ("last_item_retry_limit", 2),
                      "estimate": ("remaining_estimate_usd", "0")}[change]
        receipt[key] = value
    with pytest.raises(sj.JudgeError):
        recovery.validate(out, manifest, receipt)


async def test_only_one_last_rpc_runs_and_complete_resume_never_rejudges(tmp_path):
    plan, export, parents, out, manifest, proof = last_fixture(tmp_path)
    before = preserved_bytes(*parents)
    recovery.prepare(parents[-1], out, proof)
    retained = [row["review"] for row in first._journal(out / "calls.jsonl")]
    gateway = LastGateway(plan, export, proof["attempts"])
    result = await sj.run(out, gateway, gateway.ledger)
    assert len(gateway.requests) == 1 and len(result["items"]) == 22
    assert result["items"][:21] == retained
    assert result["items"][-1]["id"] == recovery.ITEM
    assert gateway.rows[:26] == proof["attempts"] and gateway.rows[-1]["node_id"] == "eval_call_00000027"
    assert gateway.requests[0].call.max_cost_usd_micros == 5698840
    assert result["actual_cost_usd"] is None and result["cost_known"] is False
    assert result["known_cost_usd"] == 1.003408 and result["pending_reserved_usd"] == .298752
    assert result["raw_schema_failures"] == 1
    assert json.loads((out / "budget.json").read_text()) == {
        "limit_usd_micros": 6701248, "spent_usd_micros": 1003408, "pending": False}
    assert await sj.run(out, gateway, gateway.ledger) == result
    assert len(gateway.requests) == 1 and preserved_bytes(*parents) == before
    assert json.loads((out / "manifest.json").read_text()) == manifest


async def test_unknown_last_retry_stops_without_authorizing_another_retry(tmp_path):
    plan, export, parents, out, _, proof = last_fixture(tmp_path)
    recovery.prepare(parents[-1], out, proof)
    gateway = LastGateway(plan, export, proof["attempts"])
    async def ledger():
        result = await gateway.ledger()
        if gateway.requests:
            result["attempts"][-1]["actual_usd_micros"] = None
        return result
    with pytest.raises(sj.JudgeError):
        await sj.run(out, gateway, ledger)
    assert len(gateway.requests) == 1 and not (out / "review.json").exists()
    with pytest.raises(sj.JudgeError):
        await sj.run(out, gateway, ledger)
    assert len(gateway.requests) == 1
    with pytest.raises(sj.JudgeError):
        recovery.prepare(out, tmp_path / "second-last-retry", proof)
