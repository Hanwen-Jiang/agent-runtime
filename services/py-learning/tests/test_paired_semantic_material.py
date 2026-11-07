"""Blinded evidence keeps every answer and real trace with frozen references."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from agentruntime_learning.benchmarks import paired_completion as pc, provenance as pv
from test_paired_semantic_judge import fixture

spec = importlib.util.spec_from_file_location("paired_material", Path(__file__).parents[3] / "scripts/prepare_paired_semantic_review.py")
material = importlib.util.module_from_spec(spec)
spec.loader.exec_module(material)


def prepared(tmp_path, monkeypatch):
    plan, export = fixture()
    plan["tenant_id"] = "isolated-test"
    run, tools = tmp_path / "run", tmp_path / "tools"
    run.mkdir(); tools.mkdir()
    (run / "paired-manifest.json").write_text(json.dumps(plan))
    out, refs = tmp_path / "review.json", tmp_path / "references.json"
    refs.write_text('{"facts":["Frozen primary evidence"]}')
    (tmp_path / "review-original.json").write_text(json.dumps(export))
    monkeypatch.setattr(pc, "collection_record", lambda root, plan, directory: {"task_id": directory.parent.name + "-" + directory.name})
    for arm in ("off", "rules"):
        for case in plan["cases"]:
            task = arm + "-" + case["id"]
            trace = {"root_workflow_id": task, "tenant_id": plan["tenant_id"], "complete": True,
                     "workflows": [{"calls": [{"tool": "repo_read", "request_sha256": "a" * 64,
                                                "state": "completed", "reported_started_attempt": 1}]}],
                     "scheduled_tool_requests": 1, "repeated_full_requests": 0,
                     "reported_retry_attempts_lower_bound": 0, "limitations": ["hashes are not content evidence"]}
            (tools / (task + ".json")).write_text(json.dumps(trace))
    return plan, export, (run, tools, refs, hashlib.sha256(refs.read_bytes()).hexdigest(), out)


def test_complete_material_keeps_answers_paths_sources_and_trace_hashes(tmp_path, monkeypatch):
    plan, original, args = prepared(tmp_path, monkeypatch)
    result = material.prepare(*args)
    data = json.loads(args[-1].read_text())
    assert result["items"] == 22 and result["new_model_calls"] == 0
    before = {item["id"]: item for item in original["items"]}
    assert sum(any("frozen_primary_references" in c for c in item["context"]) for item in data["items"]) == 2
    for item in data["items"]:
        assert item["actual_output"] == before[item["id"]]["actual_output"]
        assert item["planning"] == before[item["id"]]["planning"]
        assert item["tool_evidence"]["calls"][0]["request_sha256"] == "a" * 64
        assert item["evidence_sha256"] == pv.digest({k: item[k] for k in ("input", "actual_output", "context", "planning", "tool_evidence")})
    assert any(c.get("path") for item in data["items"] for c in item["context"])
    assert material.prepare(*args) == result


@pytest.mark.parametrize("change", ["reference", "foreign_trace", "incomplete_trace"])
def test_changed_or_unproven_material_cannot_enter_review(tmp_path, monkeypatch, change):
    _, _, args = prepared(tmp_path, monkeypatch)
    if change == "reference":
        args[2].write_text('{"facts":["changed"]}')
    else:
        path = next(args[1].glob("*.json")); trace = json.loads(path.read_text())
        trace["tenant_id" if change == "foreign_trace" else "complete"] = "other" if change == "foreign_trace" else False
        path.write_text(json.dumps(trace))
    with pytest.raises(ValueError):
        material.prepare(*args)
    assert not args[-1].exists()
