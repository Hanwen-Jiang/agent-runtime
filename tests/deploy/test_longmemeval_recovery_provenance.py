"""B regression: resumed measurements keep per-attempt Registry provenance."""
import copy
import json

import pytest

from test_longmemeval_recovery import accounting, fixture, recovery, successful


@pytest.mark.parametrize("mutation", [
    {"registry_revision": "v99"},
    {"registry_scope": "tenant-other"},
    {"registry_document_sha256": "c" * 64},
    {"registry_scope": None, "registry_revision": None, "registry_document_sha256": None},
])
def test_finalize_rejects_drifted_or_missing_registry_attempt(mutation, tmp_path):
    original, out, args = fixture(tmp_path)
    freeze = json.loads((original / "freeze.json").read_text())
    freeze["deployment"]["registry"] = {"profile": {"version": "v1", "document_sha256": "a" * 64}}
    (original / "freeze.json").write_text(json.dumps(freeze))
    manifest_path = original / "live/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["provenance"]["freeze_sha256"] = recovery.digest(freeze)
    manifest_path.write_text(json.dumps(manifest))
    args["observed_deployment"] = freeze["deployment"]
    recovery.prepare(original, out, **args)
    (out / "live/.run.lock").touch()
    with (out / "live/results.jsonl").open("a") as stream:
        stream.write(json.dumps(successful("b")) + "\n")
        stream.write(json.dumps(successful("c")) + "\n")
    ledger = copy.deepcopy(args["ledger"])
    new = dict(ledger["model_usage"][0], reservation_id="new-settled", actual_usd_micros=1)
    new.update(mutation)
    ledger["model_usage"].append(new)
    with pytest.raises(recovery.RecoveryError, match="Registry"):
        recovery.finalize(out, ledger=ledger)
    assert not (out / "recovery-summary.json").exists()


def test_new_settled_attempt_needs_exact_frozen_identity_without_clearing_pending():
    before = accounting()
    baseline = recovery.pending_baseline(before, {"unknown": 152}, cap_micros=19_984_709, prior_micros=15_291)
    after = copy.deepcopy(before)
    after["model_usage"].append(dict(after["model_usage"][0], reservation_id="new", actual_usd_micros=1))
    with pytest.raises(recovery.RecoveryError, match="Registry"):
        recovery.verify_ledger(after, baseline, before)
    report = recovery.verify_ledger(after, baseline, before,
                                    frozen_registry={"profile": {"version": "v1", "document_sha256": "a" * 64}})
    assert report["known_tenant_usd_micros"] == 401
    assert report["pending_reserved_usd_micros"] == 152
    assert report["actual_cost_usd"] is None and not report["cost_known"]
    assert before["model_usage"][1]["actual_usd_micros"] is None
