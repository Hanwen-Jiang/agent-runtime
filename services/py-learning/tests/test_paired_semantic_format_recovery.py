"""One uniform strict cohort retains, but never selects, the earlier scores."""
from __future__ import annotations

import copy
import hashlib
import json

from google.protobuf import json_format
import pytest

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_learning.benchmarks import paired_semantic_format_recovery as recovery
from agentruntime_learning.benchmarks import paired_semantic_recovery as first
from agentruntime_learning.benchmarks import paired_semantic_judge as sj
from agentruntime_learning.benchmarks import provenance as pv
from test_paired_semantic_judge import FakeGateway, judgment
from test_paired_semantic_recovery import original_fixture, write, journal


def parent_fixture(tmp_path):
    prepare = sj.prepare
    def legacy_prepare(*args, **kwargs):
        kwargs["schema_transport_version"] = 1
        return prepare(*args, **kwargs)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sj, "prepare", legacy_prepare)
        plan, export, original, parent, manifest, proof, estimate = original_fixture(tmp_path)
    first.prepare(original, parent, proof, estimate)
    assert export["items"][2]["id"] == recovery.BAD_ID
    calls, audit, rows = [], [], copy.deepcopy(proof["attempts"])
    spent = 0
    for sequence, item, cost in zip(range(2, 5), export["items"][:3], (34257, 35024, 47706)):
        node = f"eval_call_{sequence:08d}"
        calls.append({"id": item["id"], "state": "pending", "evidence_sha256": item["evidence_sha256"]})
        request = sj._request(manifest, export, item)
        request.tenant.tenant_id = first.TENANT
        request.call.node_id = node
        request.call.max_cost_usd_micros = 6701248 - spent
        observed = {"sequence": sequence, "state": "pending", "method": "generate",
            "task_id": manifest["task_id"], "node_id": node, "source_task_id": manifest["task_id"],
            "source_node_id": "semantic_" + item["id"], "role": request.call.role,
            "pinned_model_profile": first.PROFILE, "max_cost_usd_micros": request.call.max_cost_usd_micros,
            "request_sha256": hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()}
        audit.append(observed)
        ledger_row = {**proof["attempts"][0], "reservation_id": recovery.BAD_RID if sequence == 4 else f"known-{sequence}",
            "node_id": node, "actual_usd_micros": cost, "reserved_usd_micros": 298752,
            "reserved_at": 1794080000123 + sequence, "input_tokens": 9457 if sequence == 4 else 7000,
            "output_tokens": 739 if sequence == 4 else 500}
        rows.append(ledger_row)
        if sequence == 4:
            calls.append({"id": item["id"], "state": "failed", "error_type": "InvalidError"})
            audit.append({**observed, "state": "unresolved", "error_type": "InvalidError", "known_cost_usd_micros": cost})
        else:
            response = mg.GenerateResponse(text=json.dumps(judgment(item)), finish_reason=mg.FINISH_REASON_STOP)
            response.model.model_profile_id = first.PROFILE
            response.model.tier = "strong@medium"
            response.usage.cost_usd_micros = cost
            response.usage.input_tokens = ledger_row["input_tokens"]
            response.usage.output_tokens = ledger_row["output_tokens"]
            raw = json_format.MessageToDict(response, preserving_proto_field_name=True)
            write(parent / (item["id"] + ".response.json"), raw)
            calls.append({"id": item["id"], "state": "completed", "node_id": node, "rpc_cost_micros": cost,
                "review": sj.validate_scores(item, judgment(item)), "response_sha256": pv.digest(raw)})
            audit.append({**observed, "state": "completed", "known_cost_usd_micros": cost,
                "served_model_profile": first.PROFILE, "input_tokens": ledger_row["input_tokens"], "output_tokens": ledger_row["output_tokens"]})
        spent += cost
    journal(parent / "calls.jsonl", calls)
    journal(parent / "usage/calls.jsonl", audit)
    write(parent / "usage/identity.json", {"run_id": manifest["run_id"], "task_id": manifest["task_id"],
        "tenant_id": first.TENANT, "source_tenant_id": "evals", "gateway_address": manifest["gateway_address"], "sequence_offset": 1})
    write(parent / "budget.json", {"limit_usd_micros": 6701248, "spent_usd_micros": 116987, "pending": True})
    proof["attempts"] = rows
    return plan, export, original, parent, tmp_path / "strict-recovery", manifest, proof


def preserved_bytes(*directories):
    return {path: path.read_bytes() for directory in directories for path in directory.rglob("*")
            if path.is_file() and path.suffix in (".json", ".jsonl")}


class StrictGateway(FakeGateway):
    def __init__(self, plan, export, rows):
        super().__init__(plan, export, served=first.PROFILE)
        self.tenant = first.TENANT
        self.identity["sequence_offset"] = self.sequence = 4
        self.items = [None] * 4 + self.items
        self.rows = copy.deepcopy(rows)

    async def generate(self, request):
        response = await super().generate(request)
        self.rows[-1].update(registry_revision="v3",
            registry_document_sha256="e42df3b1eba85d89bcf869ef8942c993d469e4ca3f03818789dc1cea1bed0d5a")
        result = json.loads(response.text)
        for score in result["scores"].values():
            score.update(score=0, reason="The new uniform strict judgment is lower than the archived score.")
        response.text = json.dumps(result)
        return response


def test_prepare_preserves_originals_and_archives_old_scores_outside_the_new_cohort(tmp_path):
    _, export, original, parent, out, previous, proof = parent_fixture(tmp_path)
    before = preserved_bytes(original, parent)
    receipt = recovery.prepare(parent, out, proof)
    manifest = json.loads((out / "manifest.json").read_text())
    assert first.validate(out, manifest) == receipt
    assert manifest["schema_transport_version"] == 2 and manifest["legacy_completed_item_ids"] == []
    for key in ("run_id", "task_id", "profile", "registry", "plan_sha256", "export_sha256", "budget_usd"):
        assert manifest[key] == previous[key]
    assert all(manifest["request_sha256"][item["id"]] != previous["request_sha256"][item["id"]] for item in export["items"])
    assert receipt["sequence_offset"] == 4 and receipt["remaining_items"] == 22
    assert receipt["prior_known_usd_micros"] == 116987 and receipt["execution_cap_usd_micros"] == 6701248
    assert receipt["retained_pending"] == proof["attempts"][:1]
    assert receipt["retained_prior_known"] == proof["attempts"][1:]
    assert not (out / "calls.jsonl").exists()
    assert len(list((out / "prior-valid-responses").glob("*.response.json"))) == 2
    assert len(json.loads((out / "prior-valid-judgments.json").read_text())) == 2
    assert preserved_bytes(original, parent) == before


@pytest.mark.parametrize("change", ["known_cost", "missing_row", "parent_budget", "parent_price", "prior_response"])
def test_prepare_rejects_changed_prior_charges_evidence_or_price(tmp_path, change):
    _, export, _, parent, out, _, proof = parent_fixture(tmp_path)
    if change == "known_cost":
        proof["attempts"][1]["actual_usd_micros"] -= 1
    elif change == "missing_row":
        proof["attempts"].pop(1)
    elif change == "parent_budget":
        write(parent / "budget.json", {"limit_usd_micros": 6701248, "spent_usd_micros": 69281, "pending": True})
    elif change == "parent_price":
        plan = json.loads((parent / "plan.json").read_text())
        plan["completion"]["price_estimate"]["semantic_judge_usd"] = .001
        write(parent / "plan.json", plan)
    else:
        path = parent / (export["items"][0]["id"] + ".response.json")
        value = json.loads(path.read_text())
        value["text"] = json.dumps({"scores": {}})
        write(path, value)
    with pytest.raises(sj.JudgeError):
        recovery.prepare(parent, out, proof)


@pytest.mark.parametrize("change", ["known_cost", "missing_row", "prior_score", "prior_response", "mixed_schema", "budget", "estimate"])
def test_validate_rejects_mixed_configuration_or_modified_archived_evidence(tmp_path, change):
    _, export, _, parent, out, _, proof = parent_fixture(tmp_path)
    receipt = recovery.prepare(parent, out, proof)
    manifest = json.loads((out / "manifest.json").read_text())
    if change == "known_cost":
        receipt["retained_prior_known"][0]["actual_usd_micros"] -= 1
    elif change == "missing_row":
        receipt["retained_prior_known"].pop(0)
    elif change == "prior_score":
        path = out / "prior-valid-judgments.json"
        judgments = json.loads(path.read_text())
        next(iter(judgments[0]["review"]["scores"].values()))["score"] = 0
        write(path, judgments)
    elif change == "prior_response":
        path = out / "prior-valid-responses" / (export["items"][0]["id"] + ".response.json")
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "mixed_schema":
        manifest["legacy_completed_item_ids"] = [export["items"][0]["id"]]
        manifest["request_sha256"] = sj._request_digests(manifest, export)
        receipt["manifest_sha256"] = pv.digest(manifest)
    elif change == "budget":
        receipt["execution_cap_usd_micros"] = 7000000
    else:
        receipt["remaining_estimate_usd"] = "0"
    with pytest.raises(sj.JudgeError):
        recovery.validate(out, manifest, receipt)


async def test_all_22_uniform_judgments_replace_no_prior_score_and_keep_every_charge(tmp_path):
    plan, export, original, parent, out, previous, proof = parent_fixture(tmp_path)
    before = preserved_bytes(original, parent)
    recovery.prepare(parent, out, proof)
    gateway = StrictGateway(plan, export, proof["attempts"])
    result = await sj.run(out, gateway, gateway.ledger)
    assert len(gateway.requests) == len(result["items"]) == 22
    assert result["schema_transport_version"] == 2
    assert all(score["score"] == 0 for item in result["items"] for score in item["scores"].values())
    archived = json.loads((out / "prior-valid-judgments.json").read_text())
    assert all(score["score"] == 1 for item in archived for score in item["review"]["scores"].values())
    assert all(value == 0 for value in sj.pc.semantic_review(export, result).values())
    assert gateway.task_id == previous["task_id"]
    assert [row["node_id"] for row in gateway.rows[4:]] == [f"eval_call_{i:08d}" for i in range(5, 27)]
    assert gateway.rows[:4] == proof["attempts"]
    assert result["actual_cost_usd"] is None and result["cost_known"] is False
    assert result["known_cost_usd"] == .138987 and result["pending_reserved_usd"] == .298752
    assert result["prior_settled_attempts"] == proof["attempts"][1:]
    assert gateway.requests[0].call.max_cost_usd_micros == 6584261
    assert json.loads((out / "budget.json").read_text()) == {
        "limit_usd_micros": 6701248, "spent_usd_micros": 138987, "pending": False}
    for request in gateway.requests:
        schema = json_format.MessageToDict(request.generation.output_schema.value)
        assert "minLength" not in json.dumps(schema)
        assert all(dimension["properties"]["score"]["type"] == "number"
                   for dimension in schema["properties"]["scores"]["properties"].values())
    assert await sj.run(out, gateway, gateway.ledger) == result
    assert len(gateway.requests) == 22 and preserved_bytes(original, parent) == before


@pytest.mark.parametrize("change,calls", [("new_pending", 1), ("known_metadata", 0), ("old_pending", 0)])
async def test_new_unknown_or_changed_prior_attempt_stops_without_automatic_retry(tmp_path, change, calls):
    plan, export, original, parent, out, _, proof = parent_fixture(tmp_path)
    before = preserved_bytes(original, parent)
    recovery.prepare(parent, out, proof)
    gateway = StrictGateway(plan, export, proof["attempts"])
    async def changed_ledger():
        result = await gateway.ledger()
        if change == "new_pending" and gateway.requests:
            result["attempts"].append({**proof["attempts"][0], "reservation_id": "new-unknown", "node_id": "eval_call_00000005"})
        elif change == "known_metadata":
            result["attempts"][1]["reserved_at"] += 1
        elif change == "old_pending":
            result["attempts"][0]["reserved_usd_micros"] += 1
        return result
    with pytest.raises(sj.JudgeError):
        await sj.run(out, gateway, changed_ledger)
    assert len(gateway.requests) == calls and not (out / "review.json").exists()
    with pytest.raises(sj.JudgeError):
        await sj.run(out, gateway, changed_ledger)
    assert len(gateway.requests) == calls and preserved_bytes(original, parent) == before
