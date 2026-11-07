"""Finite recovery cannot repeat paid questions or erase accounting uncertainty."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("lme_recovery", Path(__file__).resolve().parents[2] / "scripts/recover_longmemeval_unstarted.py")
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def accounting():
    def row(identity, amount, reserved):
        return {"reservation_id": identity, "actual_usd_micros": amount, "reserved_usd_micros": reserved,
                "task_id": "", "method": "embed", "model_profile_id": "a6api/text-embedding-3-large@2026-10",
                "reserved_at": 123, "registry_scope": "platform", "registry_revision": "v1",
                "registry_document_sha256": "a" * 64}
    return {"tenant": recovery.TENANT, "missing_ledger_columns": [], "model_usage": [row("known", 400, 800), row("unknown", None, 152)],
            "queue_count": 0, "memory_item_count": 0, "lineage_count": 0, "review_count": 0,
            "tombstone_count": 0, "remaining_ids": []}


def successful(qid):
    return {"question_id": qid, "question_type": "single-session-user", "rounds": 1, "outcome": "completed",
            "cleanup_complete": True, "elapsed_s": 3.0, "session": {"recall_any@10": 1.0, "recall_all@10": 1.0, "ndcg_any@10": 1.0},
            "turn": None, "ranked_sessions": ["source"], "ranked_rounds": ["source:0"],
            "lifecycle": {"tenant": recovery.TENANT, "attempted": 2, "accepted": 2,
                          "submitted_ids": ["mem_" + "0" * 26], "cleanup_complete": True, "timed_out": False}}


def failed(qid):
    return {"question_id": qid, "question_type": "multi-session", "rounds": 1, "outcome": "environment_error",
            "error_type": "PreflightError", "cleanup_complete": True, "elapsed_s": 0.01,
            "session": {"recall_any@10": 0.0}, "turn": None, "ranked_sessions": [], "ranked_rounds": []}


def fixture(tmp_path):
    source = tmp_path / "source"
    data = tmp_path / "data.json"
    data.write_text('[{"fixed":"data"}]\n')
    original = tmp_path / "original"
    original.mkdir()
    (original / "live").mkdir()
    (original / ".launcher.lock").touch()
    (original / "live/.run.lock").touch()
    module = source / "runner.py"
    module.parent.mkdir()
    module.write_text("# immutable retrieval implementation\n")
    freeze = {"deployment": {"memory": "fixed-memory", "gateway": "fixed-gateway", "registry": "v1"},
              "data_sha256": recovery.file_hash(data), "runner_sha256": {"runner.py": recovery.file_hash(module)},
              "seed": 0, "limit": 3, "question_timeout_s": 1800, "pipeline_timeout_s": 1200,
              "remaining_allowance_usd": 19.984709, "historical_usd": .015291,
              "prior_experiment": {"actual_usd_micros": 100}}
    manifest = {"schema_version": 2, "scoring_version": "longmemeval-retrieval-v1",
                "retriever": "agentruntime_memory.eval_retriever:live_retriever", "question_ids": ["a", "b", "c"],
                "top_k": 50, "ks": [10], "budget_usd": 19.984709, "timeout_s": 1800,
                "dataset_digest": "b" * 64, "max_round_chars": 24000,
                "provenance": {"freeze_sha256": recovery.digest(freeze), "runner_sha256": freeze["runner_sha256"],
                               "data_sha256": freeze["data_sha256"], "seed": 0, "limit": 3}}
    dump(original / "freeze.json", freeze)
    dump(original / "live/manifest.json", manifest)
    dump(original / "live/summary.json", {"valid": False, "completed": 1})
    rows = [successful("a"), {**failed("b"), "outcome": "interrupted", "cleanup_complete": False, "error_type": None},
            failed("b"), failed("c")]
    (original / "live/results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    ledger = accounting()
    baseline = recovery.pending_baseline(ledger, {"unknown": 152}, cap_micros=19_984_709, prior_micros=15_291)
    args = {"observed_deployment": freeze["deployment"], "source_root": source, "data": data, "ledger": ledger,
            "baseline_ledger": ledger, "baseline": baseline}
    return original, tmp_path / "recovery", args


def test_prepare_retains_exact_completed_journal_and_all_originals(tmp_path):
    original, out, args = fixture(tmp_path)
    before = {str(p.relative_to(original)): p.read_bytes() for p in original.rglob("*") if p.is_file()}
    receipt = recovery.prepare(original, out, **args)
    assert receipt["retained_ids"] == ["a"] and receipt["unstarted_ids"] == ["b", "c"]
    rows, raw = recovery.journal(out / "live/results.jsonl")
    assert set(rows) == {"a"}
    assert raw["a"] == recovery.journal(original / "live/results.jsonl")[1]["a"]
    assert before == {str(p.relative_to(original)): p.read_bytes() for p in original.rglob("*") if p.is_file()}
    assert (out / "original-evidence/live/results.jsonl").read_bytes() == before["live/results.jsonl"]
    assert (out / "live/manifest.json").read_bytes() == before["live/manifest.json"]
    with pytest.raises(recovery.RecoveryError, match="new directory"):
        recovery.prepare(original, out, **args)


@pytest.mark.parametrize("mutate", [
    lambda row: row.update(lifecycle={}),
    lambda row: row.update(outcome="timeout", error_type="TimeoutError"),
    lambda row: row.update(outcome="interrupted", error_type="CancelledError"),
    lambda row: row.update(cost_usd=.01),
    lambda row: row.update(ranked_rounds=["already-read"]),
    lambda row: row.update(cleanup_complete=False),
])
def test_paid_or_ambiguous_failure_is_never_retried(tmp_path, mutate):
    original, out, args = fixture(tmp_path)
    rows = [json.loads(line) for line in (original / "live/results.jsonl").read_text().splitlines()]
    mutate(rows[-1])
    (original / "live/results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(recovery.RecoveryError, match="cannot be retried"):
        recovery.prepare(original, out, **args)
    assert not out.exists()


def test_earlier_lifecycle_cannot_be_hidden_by_later_preflight_placeholder(tmp_path):
    original, out, args = fixture(tmp_path)
    journal = original / "live/results.jsonl"
    journal.write_text(json.dumps({**failed("b"), "lifecycle": {"accepted": 1}}) + "\n" + journal.read_text())
    with pytest.raises(recovery.RecoveryError, match="cannot be retried"):
        recovery.prepare(original, out, **args)


@pytest.mark.parametrize("change", ["source", "data", "deployment", "manifest"])
def test_changed_frozen_configuration_cannot_merge_versions(tmp_path, change):
    original, out, args = fixture(tmp_path)
    if change == "source":
        (args["source_root"] / "runner.py").write_text("# changed\n")
    elif change == "data":
        args["data"].write_text("[]")
    elif change == "deployment":
        args["observed_deployment"] = {"memory": "new-version"}
    else:
        manifest = json.loads((original / "live/manifest.json").read_text())
        manifest["timeout_s"] = 9000
        dump(original / "live/manifest.json", manifest)
    with pytest.raises(recovery.RecoveryError):
        recovery.prepare(original, out, **args)
    assert not out.exists()


def test_pending_allowlist_is_exact_and_does_not_turn_reserved_into_actual():
    ledger = accounting()
    before = copy.deepcopy(ledger)
    baseline = recovery.pending_baseline(ledger, {"unknown": 152}, cap_micros=19_984_709, prior_micros=15_291)
    money = recovery.verify_ledger(ledger, baseline, before)
    assert money["actual_cost_usd"] is None and not money["cost_known"]
    assert money["known_tenant_usd_micros"] == 400 and money["pending_reserved_usd_micros"] == 152
    assert money["accounted_against_tenant_cap_usd_micros"] == 552 and ledger == before
    with pytest.raises(recovery.RecoveryError, match="identities"):
        recovery.pending_baseline(ledger, {"unknown": 0}, cap_micros=19_984_709, prior_micros=15_291)
    extra = {**ledger["model_usage"][1], "reservation_id": "new-unknown"}
    changed = copy.deepcopy(ledger)
    changed["model_usage"].append(extra)
    with pytest.raises(recovery.RecoveryError, match="new or changed"):
        recovery.verify_ledger(changed, baseline, before)
    changed = copy.deepcopy(ledger)
    changed["model_usage"][0]["actual_usd_micros"] = 0
    with pytest.raises(recovery.RecoveryError, match="original ledger"):
        recovery.verify_ledger(changed, baseline, before)
    changed = copy.deepcopy(ledger)
    changed["tombstone_count"] = 1
    with pytest.raises(recovery.RecoveryError, match="cleaned"):
        recovery.verify_ledger(changed, baseline, before)


def test_finalize_preserves_unknown_cost_gate_after_all_questions_complete(tmp_path):
    original, out, args = fixture(tmp_path)
    recovery.prepare(original, out, **args)
    (out / "live/.run.lock").touch()
    with (out / "live/results.jsonl").open("a") as stream:
        stream.write(json.dumps(successful("b")) + "\n")
        stream.write(json.dumps(successful("c")) + "\n")
    result = recovery.finalize(out, ledger=args["ledger"])
    assert result["completed"] == 3 and result["retrieval_complete"]
    assert result["actual_cost_usd"] is None and not result["cost_known"] and not result["valid"]
    assert result["acceptance_passed"] is False and result["recovery"]["originals_unchanged"]
    assert result["known_experiment_cost_usd"] == .0003


def test_finalize_rejects_repeated_completed_question(tmp_path):
    original, out, args = fixture(tmp_path)
    recovery.prepare(original, out, **args)
    (out / "live/.run.lock").touch()
    with (out / "live/results.jsonl").open("a") as stream:
        stream.write(json.dumps(successful("a")) + "\n")
        stream.write(json.dumps(successful("b")) + "\n")
        stream.write(json.dumps(successful("c")) + "\n")
    with pytest.raises(recovery.RecoveryError, match="altered or rerun"):
        recovery.finalize(out, ledger=args["ledger"])


def test_unrun_environment_failure_is_not_a_model_zero(tmp_path):
    original, out, args = fixture(tmp_path)
    recovery.prepare(original, out, **args)
    (out / "live/.run.lock").touch()
    with (out / "live/results.jsonl").open("a") as stream:
        stream.write(json.dumps(successful("b")) + "\n")
        stream.write(json.dumps(failed("c")) + "\n")
    result = recovery.finalize(out, ledger=args["ledger"])
    assert result["scored_questions"] == 2 and result["cohort_questions"] == 3
    assert result["session"]["recall_any@10"] == 1.0
    assert result["excluded_from_quality_as_model_failures"] == ["c"]
    assert not result["retrieval_complete"] and not result["valid"]


def test_active_original_and_recovery_output_writers_are_rejected(tmp_path):
    import fcntl
    original, out, args = fixture(tmp_path)
    with (original / ".launcher.lock").open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(recovery.RecoveryError, match="active writer"):
            recovery.prepare(original, out, **args)
        assert not out.exists()
    recovery.prepare(original, out, **args)
    (out / "live/.run.lock").touch()
    with (out / "live/.run.lock").open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(recovery.RecoveryError, match="still active"):
            recovery.finalize(out, ledger=args["ledger"])
