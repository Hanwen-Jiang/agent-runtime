"""Strict-compatible transport changes neither the rubric nor accepted scores."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re

from google.protobuf import json_format
import pytest

from agentruntime_learning.benchmarks import paired_semantic_judge as sj
from test_paired_semantic_judge import ADDRESS, TENANT, FakeGateway, fixture, judgment


def prepared(tmp_path, **options):
    plan, export = fixture()
    manifest = sj.prepare(plan, export, tmp_path, tenant_id=TENANT, gateway_address=ADDRESS, **options)
    return plan, export, manifest


def schema(manifest, export, item):
    return json_format.MessageToDict(sj._request(manifest, export, item).generation.output_schema.value)


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def test_default_transport_uses_only_actual_go_strict_compatible_keywords(tmp_path):
    _, export, manifest = prepared(tmp_path)
    assert manifest["schema_transport_version"] == 2 and manifest["legacy_completed_item_ids"] == []
    wire_source = (Path(__file__).resolve().parents[3] / "libs/go-core/adapters/openaicompat/wire.go").read_text()
    declaration = re.search(r"var strictUnsupported = \[\]string\{(.*?)\n\}", wire_source, re.S)
    assert declaration is not None
    unsupported = set(re.findall(r'"([A-Za-z]+)"', declaration.group(1)))
    assert "minLength" in unsupported
    emitted = schema(manifest, export, export["items"][0])
    for node in nodes(emitted):
        assert not unsupported.intersection(node)
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
    for dimension in emitted["properties"]["scores"]["properties"].values():
        assert dimension["properties"]["score"] == {"type": "number", "enum": [0, .5, 1]}
        assert dimension["properties"]["reason"] == {"type": "string"}


def test_explicit_v1_preserves_old_manifest_shape_and_template_bytes(tmp_path):
    plan, export, manifest = prepared(tmp_path, schema_transport_version=1)
    assert "schema_transport_version" not in manifest and "legacy_completed_item_ids" not in manifest
    before = (tmp_path / "manifest.json").read_bytes()
    original_requests = {item["id"]: sj._request(manifest, export, item).SerializeToString(deterministic=True)
                         for item in export["items"]}
    assert sj.prepare(plan, export, tmp_path, tenant_id=TENANT, gateway_address=ADDRESS,
                      schema_transport_version=1) == manifest
    assert (tmp_path / "manifest.json").read_bytes() == before
    assert all(sj._request(manifest, export, item).SerializeToString(deterministic=True) == original_requests[item["id"]]
               for item in export["items"])
    dimension = next(iter(schema(manifest, export, export["items"][0])["properties"]["scores"]["properties"].values()))
    assert dimension["properties"]["reason"]["minLength"] == 1
    assert "type" not in dimension["properties"]["score"]
    with pytest.raises(sj.JudgeError, match="prepared judge inputs changed"):
        sj.prepare(plan, export, tmp_path, tenant_id=TENANT, gateway_address=ADDRESS)


def test_two_completed_items_keep_their_exact_legacy_request_hashes(tmp_path):
    plan, export = fixture()
    old = sj.prepare(plan, export, tmp_path / "old", tenant_id=TENANT, gateway_address=ADDRESS,
                     schema_transport_version=1)
    completed = [item["id"] for item in export["items"][:2]]
    new = sj.prepare(plan, export, tmp_path / "new", tenant_id=TENANT, gateway_address=ADDRESS,
                     legacy_completed_item_ids=list(reversed(completed)))
    assert new["legacy_completed_item_ids"] == completed
    for item in export["items"]:
        assert (new["request_sha256"][item["id"]] == old["request_sha256"][item["id"]]) is (item["id"] in completed)
        before, after = sj._request(old, export, item), sj._request(new, export, item)
        assert before.generation.messages == after.generation.messages
    assert new["export_sha256"] == old["export_sha256"] and new["plan_sha256"] == old["plan_sha256"]


@pytest.mark.parametrize("version,legacy", [(0, None), (3, None), (True, None), (2.0, None),
    (2, "not-a-list"), (2, [None]), (2, ["not-in-cohort"]), (1, ["first"]), (2, ["first", "first"])])
def test_prepare_rejects_unknown_version_or_invalid_legacy_items(tmp_path, version, legacy):
    plan, export = fixture()
    if isinstance(legacy, list):
        legacy = [export["items"][0]["id"] if item == "first" else item for item in legacy]
    with pytest.raises(sj.JudgeError, match="schema transport"):
        sj.prepare(plan, export, tmp_path / "judge", tenant_id=TENANT, gateway_address=ADDRESS,
                   schema_transport_version=version, legacy_completed_item_ids=legacy)
    assert not (tmp_path / "judge").exists()


@pytest.mark.parametrize("change", ["version", "unknown_version", "legacy", "duplicate", "outside"])
async def test_changed_transport_manifest_is_rejected_before_any_rpc(tmp_path, change):
    plan, export, manifest = prepared(tmp_path)
    if change == "version":
        manifest["schema_transport_version"] = 1
    elif change == "unknown_version":
        manifest["schema_transport_version"] = 9
    else:
        first = export["items"][0]["id"]
        manifest["legacy_completed_item_ids"] = {"legacy": [first], "duplicate": [first, first], "outside": ["not-in-cohort"]}[change]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    gateway = FakeGateway(plan, export)
    with pytest.raises(sj.JudgeError):
        await sj.run(tmp_path, gateway, gateway.ledger)
    assert not gateway.requests


@pytest.mark.parametrize("field,value", [("reason", ""), ("reason", "  "), ("reason", None),
    ("score", True), ("score", .25), ("score", "1")])
def test_strict_transport_does_not_relax_nonempty_reasons_or_score_anchors(tmp_path, field, value):
    _, export, manifest = prepared(tmp_path)
    item = export["items"][0]
    result = judgment(item)
    dimension = next(iter(result["scores"]))
    assert schema(manifest, export, item)["properties"]["scores"]["properties"][dimension]["properties"]["reason"] == {"type": "string"}
    result["scores"][dimension][field] = value
    with pytest.raises(sj.JudgeError, match="invalid rubric anchor"):
        sj.validate_scores(item, result)


@pytest.mark.parametrize("anchor", [0, .5, 1])
def test_preregistered_anchors_and_evidence_identity_remain_accepted(anchor):
    _, export = fixture()
    item = export["items"][0]
    result = judgment(item)
    for value in result["scores"].values():
        value["score"] = anchor
    before = copy.deepcopy(item)
    validated = sj.validate_scores(item, result)
    assert set(validated["scores"]) == {dimension["name"] for dimension in export["rubric"]["semantic_dimensions"]}
    assert validated["evidence_sha256"] == item["evidence_sha256"] and item == before
