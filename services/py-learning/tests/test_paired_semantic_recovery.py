"""A single reviewed retry preserves unknown billing and every original byte."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from agentruntime_learning.benchmarks import paired_semantic_judge as sj
from agentruntime_learning.benchmarks import paired_semantic_recovery as recovery
from agentruntime_learning.benchmarks import provenance as pv
from test_paired_semantic_judge import ADDRESS, FakeGateway, fixture, judgment


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def journal(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def original_fixture(tmp_path):
    plan, export = fixture()
    old_run = plan["run_id"]
    plan["run_id"] = recovery.RUN
    replacements = {pv.digest([old_run, arm, case["id"]])[:24]: pv.digest([recovery.RUN, arm, case["id"]])[:24]
                    for arm in ("off", "rules") for case in plan["cases"]}
    for item in export["items"]:
        item["id"] = replacements[item["id"]]
    export["items"].sort(key=lambda item: item["id"])
    registry = {"scope": "platform", "revision": "v3",
                "document_sha256": "e42df3b1eba85d89bcf869ef8942c993d469e4ca3f03818789dc1cea1bed0d5a"}
    for arm in ("off", "rules"):
        plan["arms"][arm]["provenance"].update(semantic_judge_profile=recovery.PROFILE,
            registry={recovery.PROFILE: copy.deepcopy(registry)})
    plan["completion"]["price_estimate"]["judge_assumptions"]["output_tokens"] = 12000
    original, out = tmp_path / "original", tmp_path / "recovery"
    manifest = sj.prepare(plan, export, original, tenant_id=recovery.TENANT, gateway_address=ADDRESS,
                          max_output_tokens=12000, thinking_budget_tokens=8192)
    item = export["items"][0]
    journal(original / "calls.jsonl", [
        {"id": item["id"], "state": "pending", "evidence_sha256": item["evidence_sha256"]},
        {"id": item["id"], "state": "failed", "error_type": "ConflictError"}])
    write(original / "budget.json", {"limit_usd_micros": 7000000, "spent_usd_micros": 0, "pending": True})
    write(original / "usage/identity.json", {"run_id": manifest["run_id"], "task_id": manifest["task_id"],
        "tenant_id": recovery.TENANT, "source_tenant_id": "evals", "gateway_address": ADDRESS})
    request = sj._request(manifest, export, item)
    request.tenant.tenant_id = recovery.TENANT
    request.call.node_id = "eval_call_00000001"
    request.call.max_cost_usd_micros = 7000000
    audit = {"sequence": 1, "state": "pending", "method": "generate", "task_id": manifest["task_id"],
        "node_id": "eval_call_00000001", "source_task_id": manifest["task_id"],
        "source_node_id": "semantic_" + item["id"], "role": request.call.role,
        "pinned_model_profile": recovery.PROFILE, "max_cost_usd_micros": 7000000,
        "request_sha256": hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()}
    journal(original / "usage/calls.jsonl", [audit, {**audit, "state": "unresolved",
        "error_type": "ConflictError", "known_cost_usd_micros": None}])
    pending = {"reservation_id": recovery.PENDING, "task_id": manifest["task_id"], "node_id": "eval_call_00000001",
        "method": "generate", "model_profile_id": recovery.PROFILE, "actual_usd_micros": None,
        "reserved_usd_micros": 298752, "reserved_at": 1794080000123, "input_tokens": None, "output_tokens": None,
        "registry_scope": registry["scope"], "registry_revision": registry["revision"],
        "registry_document_sha256": registry["document_sha256"]}
    proof = {"tenant_id": recovery.TENANT, "task_id": manifest["task_id"], "attempts": [pending]}
    estimate = tmp_path / "estimate.json"
    write(estimate, {"estimated_usd": "6.511312", "request_sha256": manifest["request_sha256"]})
    return plan, export, original, out, manifest, proof, estimate


def original_bytes(original):
    return {name: (original / name).read_bytes() for name in recovery.FILES}


class RecoveryGateway(FakeGateway):
    def __init__(self, plan, export, pending):
        super().__init__(plan, export, served=recovery.PROFILE)
        self.tenant = recovery.TENANT
        self.identity["sequence_offset"] = self.sequence = 1
        self.items = [None, *self.items]
        self.rows = [copy.deepcopy(pending)]

    async def generate(self, request):
        response = await super().generate(request)
        self.rows[-1].update(registry_revision="v3",
            registry_document_sha256="e42df3b1eba85d89bcf869ef8942c993d469e4ca3f03818789dc1cea1bed0d5a")
        return response


def test_prepare_recovery_keeps_all_seven_original_files_and_the_same_manifest(tmp_path):
    _, _, original, out, manifest, proof, estimate = original_fixture(tmp_path)
    before = original_bytes(original)
    receipt = recovery.prepare(original, out, proof, estimate)
    assert recovery.validate(out, manifest) == receipt
    assert json.loads((out / "manifest.json").read_text()) == manifest
    for name in ("manifest.json", "plan.json", "export.json"):
        assert (out / name).read_bytes() == before[name]
    assert receipt["retained_pending"] == proof["attempts"]
    assert receipt["execution_cap_usd_micros"] == 6701248
    assert receipt["execution_cap_usd_micros"] + receipt["held_usd_micros"] == 7000000
    assert original_bytes(original) == before
    assert not (out / "budget.json").exists() and not (out / "calls.jsonl").exists()
    with pytest.raises(sj.JudgeError, match="separate, new"):
        recovery.prepare(original, out, proof, estimate)
    with pytest.raises(sj.JudgeError, match="separate, new"):
        recovery.prepare(original, original, proof, estimate)


@pytest.mark.parametrize("change", ["old_score", "old_response", "second_failure", "wrong_reservation", "budget",
    "estimate_over_cap", "estimate_hash", "material", "settled_old", "extra_pending", "wrong_tenant"])
def test_prepare_rejects_unreviewed_or_changed_recovery_inputs(tmp_path, change):
    _, export, original, out, manifest, proof, estimate = original_fixture(tmp_path)
    if change == "old_score":
        review = {"rubric_sha256": export["rubric_sha256"], "reviewer_version": recovery.PROFILE,
                  "items": [sj.validate_scores(item, judgment(item)) for item in export["items"]]}
        assert len(sj.pc.semantic_review(export, review)) == 22
        write(original / "review.json", review)
    elif change == "old_response":
        write(original / (manifest["item_ids"][0] + ".response.json"), {"text": "returned answer"})
    elif change == "second_failure":
        with (original / "calls.jsonl").open("a") as stream:
            stream.write(json.dumps({"id": manifest["item_ids"][1], "state": "failed", "error_type": "ConflictError"}) + "\n")
    elif change == "wrong_reservation":
        proof["attempts"][0]["reservation_id"] = "unreviewed-reservation"
    elif change == "budget":
        write(original / "budget.json", {"limit_usd_micros": 8000000, "spent_usd_micros": 0, "pending": True})
    elif change in ("estimate_over_cap", "estimate_hash"):
        value = json.loads(estimate.read_text())
        if change == "estimate_over_cap":
            value["estimated_usd"] = "6.701248000001"
        else:
            value["request_sha256"] = {}
        write(estimate, value)
    elif change == "material":
        value = json.loads((original / "export.json").read_text())
        value["items"][0]["actual_output"] = "a substituted answer"
        write(original / "export.json", value)
    elif change == "settled_old":
        proof["attempts"][0]["actual_usd_micros"] = 0
    elif change == "extra_pending":
        proof["attempts"].append({**proof["attempts"][0], "reservation_id": "extra"})
    else:
        proof["tenant_id"] = "other-tenant"
    with pytest.raises(sj.JudgeError):
        recovery.prepare(original, out, proof, estimate)
    assert not out.exists()


@pytest.mark.parametrize("name", recovery.FILES)
def test_validation_rejects_any_change_to_an_original_file(tmp_path, name):
    _, _, original, out, manifest, proof, estimate = original_fixture(tmp_path)
    recovery.prepare(original, out, proof, estimate)
    with (original / name).open("a") as stream:
        stream.write("\n")
    with pytest.raises(sj.JudgeError, match="original paid evidence changed"):
        recovery.validate(out, manifest)


@pytest.mark.parametrize("field,value", [("held_usd_micros", 0), ("execution_cap_usd_micros", 7000000),
    ("original_cap_usd_micros", 8000000), ("sequence_offset", 2), ("actual_total_cost_known", True),
    ("first_item_retry_limit", 2)])
def test_validation_cannot_release_the_hold_or_expand_retry_authority(tmp_path, field, value):
    _, _, original, out, manifest, proof, estimate = original_fixture(tmp_path)
    receipt = recovery.prepare(original, out, proof, estimate)
    receipt[field] = value
    write(out / "recovery.json", receipt)
    with pytest.raises(sj.JudgeError):
        recovery.validate(out, manifest)


async def test_recovered_22_judgments_use_new_sequences_and_never_claim_known_total_cost(tmp_path):
    plan, export, original, out, manifest, proof, estimate = original_fixture(tmp_path)
    before = original_bytes(original)
    recovery.prepare(original, out, proof, estimate)
    gateway = RecoveryGateway(plan, export, proof["attempts"][0])
    result = await sj.run(out, gateway, gateway.ledger)
    assert len(result["items"]) == len(gateway.requests) == 22
    assert gateway.task_id == manifest["task_id"]
    assert [row["node_id"] for row in gateway.rows[1:]] == [f"eval_call_{i:08d}" for i in range(2, 24)]
    assert gateway.rows[0] == proof["attempts"][0]
    assert result["actual_cost_usd"] is None and result["cost_known"] is False
    assert result["known_cost_usd"] == .022 and result["pending_reserved_usd"] == .298752
    assert result["retained_pending"] == proof["attempts"]
    assert json.loads((out / "budget.json").read_text()) == {
        "limit_usd_micros": 6701248, "spent_usd_micros": 22000, "pending": False}
    assert gateway.requests[0].call.max_cost_usd_micros == 6701248
    assert all(request.generation.max_output_tokens == 12000 and request.generation.thinking_budget_tokens == 8192
               for request in gateway.requests)
    assert await sj.run(out, gateway, gateway.ledger) == result
    assert len(gateway.requests) == 22 and original_bytes(original) == before
    final = json.loads((out / "ledger-final.json").read_text())
    assert final["attempts"][0] == proof["attempts"][0]


@pytest.mark.parametrize("change,expected_calls", [("new_pending_before", 0), ("new_pending_after", 1),
    ("old_pending_before", 0), ("old_pending_after", 1), ("old_pending_missing", 0)])
async def test_recovery_stops_on_any_new_pending_or_changed_retained_attempt(tmp_path, change, expected_calls):
    plan, export, original, out, _, proof, estimate = original_fixture(tmp_path)
    before = original_bytes(original)
    recovery.prepare(original, out, proof, estimate)
    gateway = RecoveryGateway(plan, export, proof["attempts"][0])
    async def changed_ledger():
        result = await gateway.ledger()
        if change.endswith("after") and not gateway.requests:
            return result
        if change.startswith("new_pending"):
            result["attempts"].append({**proof["attempts"][0], "reservation_id": "new-unknown",
                                       "node_id": "eval_call_00000002"})
        elif change == "old_pending_missing":
            result["attempts"].pop(0)
        else:
            result["attempts"][0]["reserved_usd_micros"] += 1
        return result
    with pytest.raises(sj.JudgeError):
        await sj.run(out, gateway, changed_ledger)
    assert len(gateway.requests) == expected_calls and not (out / "review.json").exists()
    with pytest.raises(sj.JudgeError):
        await sj.run(out, gateway, changed_ledger)
    assert len(gateway.requests) == expected_calls and original_bytes(original) == before
