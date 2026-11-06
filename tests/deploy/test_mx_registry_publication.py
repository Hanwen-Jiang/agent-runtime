"""Metadata-only Registry publication recovery; no model or live Registry calls."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/mx_registry_publication.py"
spec = importlib.util.spec_from_file_location("mx_registry_publication", SCRIPT)
assert spec and spec.loader
pub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pub)


def bundle(tmp_path):
    folder = tmp_path / "candidates/model"
    baseline = {"id": "example/model", "cost": {"in_per_m": 1}}
    candidate = {**baseline, "capability_scores": [{"role": "act", "score": 0.8}]}
    pub.save(folder / "baseline.json", {"version": "v1", "registry_sha256": "a" * 64, "document": baseline})
    pub.save(folder / "publish.json", {"name": baseline["id"], "kind": "REGISTRY_KIND_MODEL_PROFILE",
                                     "document": {"schema_id": pub.MODEL_SCHEMA, "value": candidate}})
    pub.save(tmp_path / "bundle.json", {"kind": pub.VERSION, "publication_eligible_candidates": 1,
                                      "items": [{"profile_id": baseline["id"], "directory": "candidates/model", "pricing_known": True,
                                                "files": {p.name: pub.file_digest(p) for p in folder.iterdir()}}]})
    return baseline, candidate


class FakeRegistry:
    def __init__(self, baseline, *, lose_reply=False):
        self.versions = {"v1": copy.deepcopy(baseline)}
        self.current = "v1"
        self.writes = []
        self.lose_reply = lose_reply

    def get(self, name, version=""):
        version = version or self.current
        if version not in self.versions:
            raise pub.RemoteError("not_found")
        return {"version": version, "document": {"value": self.versions[version]}}

    def call(self, method, request):
        assert method == "Publish", "the preparation script must never call Promote"
        self.writes.append(copy.deepcopy(request))
        version = f"v{len(self.versions) + 1}"
        self.versions[version] = request["document"]["value"]
        if self.lose_reply:
            raise pub.RemoteError("response_unknown")
        return {"name": request["name"], "version": version}


def test_preflight_is_read_only_and_default_drift_rejects_before_write(tmp_path):
    baseline, _ = bundle(tmp_path)
    remote = FakeRegistry(baseline)
    pub.publish_shadows(tmp_path, remote)
    assert remote.writes == []
    remote.versions["v1"]["cost"]["in_per_m"] = 99
    with pytest.raises(ValueError, match="drifted"):
        pub.publish_shadows(tmp_path, remote, execute=True)
    assert remote.writes == []


def test_lost_publish_ack_is_recovered_without_duplicate_version(tmp_path):
    baseline, _ = bundle(tmp_path)
    remote = FakeRegistry(baseline, lose_reply=True)
    with pytest.raises(pub.RemoteError):
        pub.publish_shadows(tmp_path, remote, execute=True)
    assert json.loads((tmp_path / "publication-state.json").read_text())["profiles"][baseline["id"]]["state"] == "pending"
    result = pub.publish_shadows(tmp_path, remote, execute=True)
    assert len(remote.writes) == 1
    assert result["profiles"][baseline["id"]] == {"state": "shadow_found", "version": "v2"}
    assert remote.current == "v1"


def test_unknown_publish_not_yet_found_is_not_retried(tmp_path):
    baseline, _ = bundle(tmp_path)
    remote = FakeRegistry(baseline)
    pub.save(tmp_path / "publication-state.json", {"bundle_sha256": pub.file_digest(tmp_path / "bundle.json"),
                                                "profiles": {baseline["id"]: {"state": "pending"}}})
    with pytest.raises(ValueError, match="reconcile"):
        pub.publish_shadows(tmp_path, remote, execute=True)
    assert remote.writes == []


def test_mutated_payload_is_rejected_before_any_rpc(tmp_path):
    baseline, _ = bundle(tmp_path)
    remote = FakeRegistry(baseline)
    (tmp_path / "candidates/model/publish.json").write_text("{}")
    with pytest.raises(ValueError, match="changed"):
        pub.publish_shadows(tmp_path, remote, execute=True)
    assert remote.writes == []


def test_unrelated_shadow_is_not_overwritten_or_promoted(tmp_path):
    baseline, candidate = bundle(tmp_path)
    remote = FakeRegistry(baseline)
    remote.versions["v2"] = {**baseline, "unrelated": True}
    result = pub.publish_shadows(tmp_path, remote, execute=True)
    assert result["profiles"][baseline["id"]]["version"] == "v3"
    assert remote.versions["v3"] == candidate and remote.versions["v2"]["unrelated"]
    assert remote.current == "v1"


def test_quality_only_draft_cannot_bypass_cost_gate(tmp_path):
    baseline, _ = bundle(tmp_path)
    path = tmp_path / "bundle.json"
    doc = json.loads(path.read_text())
    del doc["publication_eligible_candidates"]
    pub.save(path, doc)
    remote = FakeRegistry(baseline)
    with pytest.raises(ValueError, match="cost-audited"):
        pub.publish_shadows(tmp_path, remote, execute=True)
    assert remote.writes == []


def promotion_bundle(tmp_path):
    baseline, candidate = bundle(tmp_path)
    folder = tmp_path / "candidates/model"
    pub.save(folder / "report.json", {"quality": "fixed evidence"})
    pub.save(folder / "promote-request-template.json", {
        "kind": "REGISTRY_KIND_MODEL_PROFILE", "name": baseline["id"], "version": "<PUBLISHED_VERSION>",
        "evaluation": {"schema_id": "https://agentruntime.example/schemas/registry_capability_evaluation.schema.json",
                       "value": {"passed": True, "baseline_version": "v1", "report_sha256": pub.file_digest(folder / "report.json")}},
    })
    doc = json.loads((tmp_path / "bundle.json").read_text())
    doc["items"][0]["files"] = {p.name: pub.file_digest(p) for p in folder.iterdir()}
    pub.save(tmp_path / "bundle.json", doc)
    pub.save(tmp_path / "publication-state.json", {
        "bundle_sha256": pub.file_digest(tmp_path / "bundle.json"),
        "profiles": {baseline["id"]: {"state": "published_shadow", "version": "v2"}},
    })
    return baseline, candidate


class PromotingRegistry(FakeRegistry):
    def __init__(self, baseline, candidate, **kwargs):
        super().__init__(baseline, **kwargs)
        self.versions["v2"] = candidate

    def call(self, method, request):
        assert method == "Promote"
        self.writes.append(copy.deepcopy(request))
        previous, self.current = self.current, request["version"]
        if self.lose_reply:
            raise pub.RemoteError("response_unknown")
        return {"previousDefaultVersion": previous}


def test_promotion_dry_run_and_lost_reply_preserve_rollback(tmp_path):
    base, candidate = promotion_bundle(tmp_path)
    remote = PromotingRegistry(base, candidate, lose_reply=True)
    pub.promote_shadows(tmp_path, remote)
    assert remote.current == "v1" and not remote.writes
    with pytest.raises(pub.RemoteError):
        pub.promote_shadows(tmp_path, remote, execute=True)
    result = pub.promote_shadows(tmp_path, remote, execute=True)
    assert len(remote.writes) == 1 and remote.current == "v2"
    assert result["profiles"][base["id"]] == {"state": "default_confirmed", "version": "v2", "previous_default_version": "v1"}


def test_promotion_never_overwrites_a_drifted_default(tmp_path):
    base, candidate = promotion_bundle(tmp_path)
    remote = PromotingRegistry(base, candidate)
    remote.versions["v3"] = {**base, "cost": {"in_per_m": 99}}
    remote.current = "v3"
    with pytest.raises(ValueError, match="drifted"):
        pub.promote_shadows(tmp_path, remote, execute=True)
    assert not remote.writes


def test_promotion_rejects_modified_evidence(tmp_path):
    base, candidate = promotion_bundle(tmp_path)
    remote = PromotingRegistry(base, candidate)
    (tmp_path / "candidates/model/report.json").write_text("{}")
    with pytest.raises(ValueError, match="changed"):
        pub.promote_shadows(tmp_path, remote, execute=True)
    assert not remote.writes
