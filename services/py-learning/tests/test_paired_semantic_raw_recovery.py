"""Extraction preserves failed raw validation and reuses five billed responses."""
from __future__ import annotations

import copy
import hashlib
import json

from google.protobuf import json_format
import pytest

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_learning.benchmarks import paired_semantic_judge as sj
from agentruntime_learning.benchmarks import paired_semantic_recovery as first
from agentruntime_learning.benchmarks import paired_semantic_format_recovery as formatted
from agentruntime_learning.benchmarks import paired_semantic_raw_recovery as recovery
from agentruntime_learning.benchmarks import provenance as pv
from test_paired_semantic_format_recovery import parent_fixture, preserved_bytes
from test_paired_semantic_judge import FakeGateway, judgment
from test_paired_semantic_recovery import write, journal


def raw_fixture(tmp_path, *, anchor=1):
    plan, export, original, legacy, parent, _, proof = parent_fixture(tmp_path)
    formatted.prepare(legacy, parent, proof)
    manifest = json.loads((parent / "manifest.json").read_text())
    calls, audit, rows = [], [], copy.deepcopy(proof["attempts"])
    assert export["items"][4]["id"] == recovery.ITEM
    spent = 116987
    for sequence, item, cost in zip(range(5, 10), export["items"][:5], (37600, 37210, 47575, 35327, 57578)):
        result = judgment(item)
        if sequence == 9:
            for score in result["scores"].values():
                score["score"] = anchor
            result["overall_assessment"] = "Unscored assessment retained in the original response."
        response = mg.GenerateResponse(text=json.dumps(result), finish_reason=mg.FINISH_REASON_STOP)
        json_format.ParseDict(result, response.result)
        response.model.model_profile_id = first.PROFILE
        response.model.tier = "strong@medium"
        response.usage.cost_usd_micros = cost
        response.usage.input_tokens = 10757 if sequence == 9 else 7000
        response.usage.output_tokens = 942 if sequence == 9 else 500
        raw = json_format.MessageToDict(response, preserving_proto_field_name=True)
        write(parent / (item["id"] + ".response.json"), raw)
        node = f"eval_call_{sequence:08d}"
        calls.append({"id": item["id"], "state": "pending", "evidence_sha256": item["evidence_sha256"]})
        if sequence == 9:
            calls.append({"id": item["id"], "state": "failed", "error_type": "JudgeError"})
        else:
            calls.append({"id": item["id"], "state": "completed", "node_id": node,
                "rpc_cost_micros": cost, "review": sj.validate_scores(item, result), "response_sha256": pv.digest(raw)})
        request = sj._request(manifest, export, item)
        request.tenant.tenant_id = first.TENANT
        request.call.node_id = node
        request.call.max_cost_usd_micros = 6701248 - spent
        entry = {"sequence": sequence, "state": "pending", "method": "generate", "task_id": manifest["task_id"],
            "node_id": node, "source_node_id": "semantic_" + item["id"], "source_task_id": manifest["task_id"],
            "request_sha256": hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest(),
            "max_cost_usd_micros": request.call.max_cost_usd_micros, "pinned_model_profile": first.PROFILE,
            "role": request.call.role}
        audit.extend([entry, {**entry, "state": "completed", "known_cost_usd_micros": cost,
            "served_model_profile": first.PROFILE, "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens}])
        rows.append({**rows[0], "reservation_id": recovery.RESERVATION if sequence == 9 else f"strict-{sequence}",
            "node_id": node, "actual_usd_micros": cost, "reserved_at": 1794080010000 + sequence,
            "input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens})
        spent += cost
    journal(parent / "calls.jsonl", calls)
    journal(parent / "usage/calls.jsonl", audit)
    write(parent / "usage/identity.json", {"run_id": manifest["run_id"], "task_id": manifest["task_id"],
        "tenant_id": first.TENANT, "source_tenant_id": "evals", "gateway_address": manifest["gateway_address"], "sequence_offset": 4})
    write(parent / "budget.json", {"limit_usd_micros": 6701248, "spent_usd_micros": 332277, "pending": False})
    proof["attempts"] = rows
    return plan, export, (original, legacy, parent), tmp_path / "extracted", manifest, proof


class RemainingGateway(FakeGateway):
    def __init__(self, plan, export, rows):
        super().__init__(plan, export, served=first.PROFILE)
        self.tenant = first.TENANT
        self.identity["sequence_offset"] = self.sequence = 9
        self.items = [None] * 9 + self.items[5:]
        self.rows = copy.deepcopy(rows)

    async def generate(self, request):
        response = await super().generate(request)
        self.rows[-1].update(registry_revision="v3",
            registry_document_sha256="e42df3b1eba85d89bcf869ef8942c993d469e4ca3f03818789dc1cea1bed0d5a")
        return response


@pytest.mark.parametrize("anchor", [0, .5, 1])
def test_prepare_extracts_without_regrading_or_changing_any_original_bytes(tmp_path, anchor):
    _, export, parents, out, manifest, proof = raw_fixture(tmp_path, anchor=anchor)
    before = preserved_bytes(*parents)
    source = parents[-1] / (recovery.ITEM + ".response.json")
    result = json.loads(source.read_text())["result"]
    with pytest.raises(sj.JudgeError):
        sj.validate_scores(export["items"][4], result)
    receipt = recovery.prepare(parents[-1], out, proof)
    assert first.validate(out, manifest) == receipt
    assert json.loads((out / "manifest.json").read_text()) == manifest
    records = sj._history(out)
    assert len(records) == 5 and sum(row["rpc_cost_micros"] for row in records.values()) == 215290
    derived = records[recovery.ITEM]
    assert derived["review"]["scores"] == result["scores"]
    assert all(score["score"] == anchor for score in derived["review"]["scores"].values())
    assert derived["raw_schema_valid"] is False and derived["ignored_root_fields"] == ["overall_assessment"]
    assert derived["result_extraction_version"] == "paired-score-extraction-v1"
    assert derived["rpc_cost_micros"] == 57578
    assert receipt["sequence_offset"] == 9 and receipt["new_model_calls_for_recovery"] == 0
    assert not (out / "budget.json").exists() and not (out / "usage/calls.jsonl").exists()
    assert (out / source.name).read_bytes() == source.read_bytes()
    assert preserved_bytes(*parents) == before
    for old in first._journal(parents[-1] / "calls.jsonl")[1:8:2]:
        assert records[old["id"]] == old


@pytest.mark.parametrize("change", ["unknown_extra", "nonstring_assessment", "invalid_score", "empty_reason", "missing_dimension"])
def test_extractor_still_rejects_every_change_to_scored_content_or_unknown_extra(tmp_path, change):
    _, _, parents, out, _, proof = raw_fixture(tmp_path)
    path = parents[-1] / (recovery.ITEM + ".response.json")
    raw = json.loads(path.read_text())
    scores = raw["result"]["scores"]
    if change == "unknown_extra":
        raw["result"]["another_assessment"] = "not allowlisted"
    elif change == "nonstring_assessment":
        raw["result"]["overall_assessment"] = {"score": 1}
    elif change == "invalid_score":
        next(iter(scores.values()))["score"] = .25
    elif change == "empty_reason":
        next(iter(scores.values()))["reason"] = "  "
    else:
        scores.pop(next(iter(scores)))
    write(path, raw)
    with pytest.raises(sj.JudgeError):
        recovery.prepare(parents[-1], out, proof)
    assert not out.exists()


@pytest.mark.parametrize("change", ["missing_row", "wrong_reservation", "wrong_fee", "new_pending", "pending_budget"])
def test_prepare_cannot_extract_without_the_complete_settled_nine_attempt_ledger(tmp_path, change):
    _, _, parents, out, _, proof = raw_fixture(tmp_path)
    if change == "missing_row":
        proof["attempts"].pop(1)
    elif change == "wrong_reservation":
        proof["attempts"][-1]["reservation_id"] = "another"
    elif change == "wrong_fee":
        proof["attempts"][-1]["actual_usd_micros"] -= 1
    elif change == "new_pending":
        proof["attempts"][-1]["actual_usd_micros"] = None
    else:
        write(parents[-1] / "budget.json", {"limit_usd_micros": 6701248, "spent_usd_micros": 332277, "pending": True})
    with pytest.raises(sj.JudgeError):
        recovery.prepare(parents[-1], out, proof)


@pytest.mark.parametrize("change", ["parent_journal", "copied_raw", "seed_score", "ledger_snapshot", "offset", "refund", "estimate"])
def test_validation_detects_tampered_source_derived_records_or_allowance(tmp_path, change):
    _, _, parents, out, manifest, proof = raw_fixture(tmp_path)
    receipt = recovery.prepare(parents[-1], out, proof)
    if change == "parent_journal":
        path = parents[-1] / "calls.jsonl"
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "copied_raw":
        path = out / (recovery.ITEM + ".response.json")
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "seed_score":
        rows = first._journal(out / "calls.jsonl")
        next(iter(rows[-1]["review"]["scores"].values()))["score"] = 0
        journal(out / "calls.jsonl", rows)
    elif change == "ledger_snapshot":
        value = json.loads((out / "parent-ledger.json").read_text())
        value["attempts"][-1]["reserved_at"] += 1
        write(out / "parent-ledger.json", value)
    else:
        key, value = {"offset": ("sequence_offset", 10), "refund": ("original_spent_usd_micros", 274699),
                      "estimate": ("remaining_estimate_usd", "0")}[change]
        receipt[key] = value
    with pytest.raises(sj.JudgeError):
        recovery.validate(out, manifest, receipt)


async def test_reuses_five_billed_responses_and_runs_only_remaining_17_once(tmp_path):
    plan, export, parents, out, manifest, proof = raw_fixture(tmp_path, anchor=.5)
    before = preserved_bytes(*parents)
    recovery.prepare(parents[-1], out, proof)
    gateway = RemainingGateway(plan, export, proof["attempts"])
    assert not gateway.requests
    result = await sj.run(out, gateway, gateway.ledger)
    assert len(result["items"]) == 22 and len(gateway.requests) == 17
    assert [row["node_id"] for row in gateway.rows[9:]] == [f"eval_call_{n:08d}" for n in range(10, 27)]
    assert gateway.task_id == manifest["task_id"] and gateway.rows[:9] == proof["attempts"]
    assert result["actual_cost_usd"] is None and result["cost_known"] is False
    assert result["known_cost_usd"] == .349277 and result["pending_reserved_usd"] == .298752
    assert result["raw_schema_failures"] == 1 and result["raw_schema_failure_ids"] == [recovery.ITEM]
    assert result["result_extraction_version"] == "paired-score-extraction-v1"
    retained = next(item for item in result["items"] if item["id"] == recovery.ITEM)
    assert all(score["score"] == .5 for score in retained["scores"].values())
    assert gateway.requests[0].call.max_cost_usd_micros == 6368971
    assert json.loads((out / "budget.json").read_text()) == {
        "limit_usd_micros": 6701248, "spent_usd_micros": 349277, "pending": False}
    assert await sj.run(out, gateway, gateway.ledger) == result
    assert len(gateway.requests) == 17 and preserved_bytes(*parents) == before


async def test_new_pending_stops_the_remaining_items_and_cannot_auto_retry(tmp_path):
    plan, export, parents, out, _, proof = raw_fixture(tmp_path)
    recovery.prepare(parents[-1], out, proof)
    gateway = RemainingGateway(plan, export, proof["attempts"])
    async def changed_ledger():
        value = await gateway.ledger()
        if gateway.requests:
            value["attempts"].append({**proof["attempts"][0], "reservation_id": "new-pending", "node_id": "eval_call_00000010"})
        return value
    with pytest.raises(sj.JudgeError):
        await sj.run(out, gateway, changed_ledger)
    assert len(gateway.requests) == 1 and not (out / "review.json").exists()
    with pytest.raises(sj.JudgeError):
        await sj.run(out, gateway, changed_ledger)
    assert len(gateway.requests) == 1
