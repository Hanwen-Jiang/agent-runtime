#!/usr/bin/env python3
"""Prepare all fixed paired answers for one blinded review, without model calls."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from agentruntime_learning.benchmarks import paired_completion as pc, provenance as pv


def prepare(run: Path, tools: Path, references: Path, reference_sha256: str, out: Path) -> dict:
    reference_bytes = references.read_bytes()
    if hashlib.sha256(reference_bytes).hexdigest() != reference_sha256:
        raise ValueError("frozen primary references changed")
    reference = json.loads(reference_bytes)
    plan = json.loads((run / "paired-manifest.json").read_text())
    # Preserve the original complete export alongside the supplemented material.
    raw_path = out.with_name(out.stem + "-original.json")
    if not raw_path.exists():
        pc.review_export(run, raw_path)
    export = json.loads(raw_path.read_text())
    items = {item["id"]: item for item in export["items"]}
    expected = {pv.digest([plan["run_id"], arm, case["id"]])[:24]
                for arm in ("off", "rules") for case in plan["cases"]}
    if len(items) != len(export["items"]) or set(items) != expected:
        raise ValueError("every original pair must be retained exactly once")
    source_hashes = {}
    for case in plan["cases"]:
        for arm in ("off", "rules"):
            item = items[pv.digest([plan["run_id"], arm, case["id"]])[:24]]
            metric = pc.collection_record(run, plan, run / arm / case["id"])
            path = tools / (metric["task_id"] + ".json")
            trace = json.loads(path.read_text())
            if trace.get("root_workflow_id") != metric["task_id"] or trace.get("tenant_id") != plan["tenant_id"] or trace.get("complete") is not True:
                raise ValueError("complete real tool history for this Task is required")
            source_hashes[item["id"]] = hashlib.sha256(path.read_bytes()).hexdigest()
            item["context"] = [{"path": f["path"], "content": f["content"]} for f in case.get("files", [])]
            if case["id"] == pc.MANDATORY_ID:
                item["context"].append({"frozen_primary_references": reference})
            # Keep all real tool decisions and outcomes; omit arm/task labels.
            # Argument hashes support exact repetition counts, not content claims.
            item["tool_evidence"] = {
                "source": "complete Temporal activity histories; argument hashes only",
                "calls": [{key: call.get(key) for key in ("tool", "request_sha256", "state", "reported_started_attempt")}
                          for workflow in trace["workflows"] for call in workflow["calls"]],
                "scheduled_tool_requests": trace["scheduled_tool_requests"],
                "repeated_full_requests": trace["repeated_full_requests"],
                "reported_retry_attempts_lower_bound": trace["reported_retry_attempts_lower_bound"],
                "limitations": trace["limitations"],
            }
            body = {key: item[key] for key in ("input", "actual_output", "context", "planning", "tool_evidence")}
            item["evidence_sha256"] = pv.digest(body)
    export["material_provenance"] = {
        "original_export_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "reference_sha256": reference_sha256, "tool_history_sha256": source_hashes,
        "plan_sha256": pv.digest(plan), "new_model_calls": 0,
        "same_vendor_limitation": "The Opus semantic judge and Sonnet planner share a vendor in both arms.",
    }
    content = json.dumps(export, ensure_ascii=False, indent=2) + "\n"
    if out.exists() and out.read_text() != content:
        raise ValueError("prepared review changed; preserve the previous evidence")
    if not out.exists():
        out.write_text(content)
    return {"items": len(items), "reference_sha256": reference_sha256,
            "export_sha256": hashlib.sha256(out.read_bytes()).hexdigest(), "new_model_calls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--reference-sha256", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.run, args.tools, args.references, args.reference_sha256, args.out)))
