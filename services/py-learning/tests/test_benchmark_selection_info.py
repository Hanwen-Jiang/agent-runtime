"""Public run provenance: preserve exact cohorts, without private inputs or inferred sampling."""
from __future__ import annotations

import json
from pathlib import Path

from agentruntime_learning.benchmarks.__main__ import _run_cohort
from agentruntime_learning.evals.public_benchmarks import PublicBenchmarkSource, VERIFIED_EXPORT, project_benchmark
from agentruntime_learning.evals.public_terminalbench import PublicTerminalBenchSource


def test_live_runs_export_counts_not_ids_and_resumes_keep_original_selector(tmp_path: Path):
    ids = ["django__django-11276", "sympy__sympy-12096"]
    info = _run_cohort("swebench", tmp_path, ids, {"instances_sha256": VERIFIED_EXPORT, "limit": 2,
                                                   "stratified": True, "seed": 0, "exclude_repos": ["psf/requests"]})
    assert info == {"dataset": "SWE-bench Verified", "method": "stratified_repo_difficulty",
                    "selected_count": 2, "population_count": 500, "eligible_count": 492, "excluded_count": 8, "seed": 0}
    frozen = {"instance_ids": ["task-a", "task-b"], "selection": {"limit": 2, "seed": 8, "population_count": 89,
                                                                   "eligible_count": 3, "categories": ["internal"]}}
    (tmp_path / "run.json").write_text(json.dumps(frozen))
    assert _run_cohort("terminal-bench", tmp_path, frozen["instance_ids"], {"ids_file": "/private/ids"}) == {
        "dataset": "Terminal-Bench 2.0", "method": "seeded_random", "selected_count": 2,
        "population_count": 89, "eligible_count": 3, "excluded_count": 86, "seed": 8}


def _swe(selection, ids):
    run = {"benchmark": "swe-bench", "run_id": "run-1", "created_at": "2026-01-01T00:00:00Z",
           "instance_ids": ids, "settings": {"environment": "official", "hints": False, "model_name": "m"},
           "selection": selection}
    rows = [{"instance_id": i, "difficulty": "<15 min fix", "state": "finished", "grading": "unresolved"} for i in ids]
    report = {"benchmark": "swe-bench", "run_id": "run-1", "model_name": "m", "selected": len(ids),
              "resolved": 0, "resolve_rate": 0, "graded": True, "instances": rows}
    return project_benchmark(run, report, "a" * 64)["selection_info"]


def test_swe_official_counts_and_frozen_tree_from_matching_run(tmp_path):
    path = tmp_path / "attempt"
    path.mkdir()
    labels = ["resolved", "unresolved", "empty_patch", "run_error", "error"]
    rows = [{"instance_id": f"test-{i}", "grading": label, "state": "error" if label == "run_error" else "finished"}
            for i, label in enumerate(labels)]
    run = {"run_id": "experiment", "benchmark": "swe-bench", "created_at": "2026-01-01T00:00:00Z",
           "instance_ids": [row["instance_id"] for row in rows],
           "settings": {"hints": False, "environment": "official", "model_name": "model"}}
    report = {"run_id": "experiment", "benchmark": "swe-bench", "instances": rows, "graded": True,
              "selected": 5, "resolved": 1, "resolve_rate": .2, "ran": 4, "not_run": 0, "errors": 1, "model_name": "model"}
    (path / "run.json").write_text(json.dumps(run))
    (path / "report.json").write_text(json.dumps(report))
    def read_row():
        return json.loads(PublicBenchmarkSource(tmp_path).get())["runs"][0]
    source = read_row()
    assert (source["graded_count"], source["empty_patch_count"], source["grading_errors"], source["errors"]) == (2, 1, 1, 1)
    assert source["scores"]["resolved_rate"] == .2 and "runtime_tree" not in source
    protocol = {"run_id": "experiment", "model_label": "model", "runtime_tree": "d" * 40}
    (path / "paired-protocol.json").write_text(json.dumps(protocol))
    assert read_row()["runtime_tree"] == protocol["runtime_tree"]
    (path / "paired-protocol.json").write_text(json.dumps({**protocol, "run_id": "other"}))
    assert "runtime_tree" not in read_row()


def test_quota_interruption_requires_task_evidence_and_retains_official_denominator(tmp_path):
    path = tmp_path / "pilot"
    records = path / "records"
    records.mkdir(parents=True)
    ids = [f"org__repo-{i}" for i in range(1,6)]
    statuses = ["aborted", "cancelled", "aborted", "aborted", "done"]
    rows = [{"instance_id": i, "state": "finished", "status": s,
             "grading": "resolved" if s == "done" else "empty_patch"} for i,s in zip(ids,statuses)]
    run = {"run_id": "pilot", "benchmark": "swe-bench", "created_at": "2026-01-01T00:00:00Z",
           "instance_ids": ids, "settings": {"hints": False, "environment": "official", "model_name": "m"}}
    report = {"run_id": "pilot", "benchmark": "swe-bench", "instances": rows, "graded": True, "graded_complete": True,
              "selected": 5, "resolved": 1, "resolve_rate": .2, "ran": 5, "not_run": 0, "errors": 0, "model_name": "m"}
    (path / "run.json").write_text(json.dumps(run))
    (path / "report.json").write_text(json.dumps(report))
    evidence = {"codes":{"nodes":[{"error":"tool_loop: insufficient_user_quota: 模型供应商返回 403：当前可用额度不足。"}]}}
    for i,(case_id,status) in enumerate(zip(ids,statuses)):
        record = {"instance_id":case_id,"status":status,"task_finished":True,"task_id":f"t-{i}","patch":""}
        if i == 1:
            record["provider_failure"] = {"code":"insufficient_user_quota","event_id":21,"source":"temporal_activity_failure","task_id":"t-1"}
        elif i == 2:
            record["deliverable"] = "insufficient_user_quota: 模型供应商返回 403"
        else:
            record["deliverable"] = json.dumps(evidence)
        output = records / (case_id + ".json")
        if i == 3:
            outside = tmp_path / "foreign.json"
            outside.write_text(json.dumps(record))
            output.symlink_to(outside)
        else:
            output.write_text(json.dumps(record))
    raw = PublicBenchmarkSource(tmp_path).get()
    row = json.loads(raw)["runs"][0]
    assert row["status"] == "partial" and not row["complete"] and not row["graded_complete"]
    assert (row["provider_quota_interrupted_count"],row["resolved"],row["selected"],row["graded_count"]) == (2,1,5,1)
    assert "insufficient_user_quota" not in raw.decode()


def test_swe_stratified_archive_explains_the_pool_and_relates_every_public_id():
    ids = ["django__django-11276", "sympy__sympy-12096"]
    info = _swe({"instances_sha256": VERIFIED_EXPORT, "instances_file": "/private/input/verified.jsonl",
                 "stratified": True, "seed": 0, "limit": 2, "exclude_repos": ["psf/requests"]}, ids)
    assert (info["method"], info["population_count"], info["eligible_count"], info["excluded_count"]) == (
        "stratified_repo_difficulty", 500, 492, 8)
    assert info["selection_ids"] == ids and info["seed"] == 0
    assert info["repo_counts"] == {"django/django": 1, "sympy/sympy": 1}
    assert info["difficulty_counts"] == {"<15 min fix": 2}
    assert "/private/" not in json.dumps(info)


def test_private_swe_manifest_never_copies_paths_or_ids_or_assumes_verified_population():
    info = _swe({"instances_file": "/secret/verified.jsonl", "ids_file": "/secret/selected.txt",
                 "instances_sha256": "a" * 64, "stratified": True, "seed": 0, "limit": 1},
                ["sensitive_holdout_id"])
    assert info["method"] == "explicit_ids" and info["selected_count"] == 1
    assert "seed" not in info and "population_count" not in info and "selection_ids" not in info
    assert "secret" not in str(info) and "sensitive_holdout" not in str(info)


def test_terminal_summary_without_selector_reports_ids_and_only_uses_matching_manifest(tmp_path: Path):
    path = tmp_path / "tb20-test"
    path.mkdir()
    ids = ["adaptive-rejection-sampler", "configure-git-webserver"]
    data = {"schema": "terminalbench-summary-v1", "benchmark": "terminal-bench", "run_id": "tb20-test",
            "model_name": "agent-runtime", "protocol": "runtime-workdir-official-tests-v2-binary", "dataset_version": "2.0",
            "selected_ids": ids, "cases": [{"instance_id": i, "outcome": "not_run"} for i in ids],
            "dataset_sha256": "a" * 64, "completed_at": "2026-01-01T00:00:00Z", "status": "partial",
            "cost_scope": "cohort", "cost_known": False, "known_cost_usd": 0, "pending_reserved_usd": 0}
    (path / "public-summary.json").write_text(json.dumps(data))
    source = PublicTerminalBenchSource(tmp_path, ttl_s=0)
    unknown = source.get()["runs"][0]
    assert unknown["selection_info"]["method"] == "unrecorded"
    assert unknown["selection_info"]["selection_ids"] == ids
    assert unknown["selection_info"]["selected_count"] == 2 and unknown["graded_count"] == 0
    manifest = {"benchmark": "terminal-bench", "run_id": "tb20-test", "instance_ids": ids, "dataset_sha256": "b" * 64,
                "selection": {"ids_file": "/private/selected.ids", "portable_only": True, "seed": 0}}
    (path / "run.json").write_text(json.dumps(manifest))
    assert source.get()["runs"][0]["selection_info"] == unknown["selection_info"]
    manifest["dataset_sha256"] = data["dataset_sha256"]
    (path / "run.json").write_text(json.dumps(manifest))
    known = source.get()["runs"][0]["selection_info"]
    assert known["method"] == "explicit_ids" and "seed" not in known and "portable_only" not in known
    assert "/private/" not in str(known)
    data["selection"] = {"population_count": 89, "eligible_count": 14, "limit": 2, "seed": 17,
                         "categories": ["software-engineering"], "portable_only": True}
    (path / "public-summary.json").write_text(json.dumps(data))
    known = source.get()["runs"][0]["selection_info"]
    assert (known["method"], known["seed"], known["eligible_count"], known["categories"]) == (
        "seeded_random", 17, 14, ["software-engineering"])
