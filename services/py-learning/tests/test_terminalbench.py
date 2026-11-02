import json

import pytest

from agentruntime_learning.benchmarks import terminalbench as tb


def test_smoke_manifest_is_fixed_five_tasks():
    manifest = tb.smoke_manifest()
    assert manifest["dataset_version"] == "terminal-bench-2.0"
    assert [t["task_id"] for t in manifest["tasks"]] == [f"tbench-2.0-smoke-{i:02d}" for i in range(1, 6)]


def test_fixture_and_oracle_share_verifier_contract(tmp_path):
    manifest = tb.smoke_manifest()
    manifest["tasks"][0]["verifier"] = {"status": "passed", "reward": 1.0}
    fixture = tb.run(manifest, tmp_path / "fixture", budget_usd=20, mode="fixture")
    oracle = tb.run(manifest, tmp_path / "oracle", budget_usd=20, mode="oracle")
    assert [(r.verifier_status, r.reward) for r in fixture] == [(r.verifier_status, r.reward) for r in oracle]
    assert json.loads((tmp_path / "fixture" / "terminalbench-report.json").read_text())["mode"] == "fixture"


def test_harbor_mode_has_explicit_preflight_failure(monkeypatch):
    monkeypatch.setattr(tb.importlib, "import_module", lambda _: (_ for _ in ()).throw(ImportError()))
    ok, message = tb.preflight(require_harbor=True)
    assert not ok and "harbor 0.24.0" in message
    with pytest.raises(RuntimeError, match="preflight_failed"):
        tb.run(tb.smoke_manifest(), __import__("pathlib").Path("out"), budget_usd=20, mode="harbor")


def test_harbor_mode_requires_runtime_bridge_command(monkeypatch, tmp_path):
    monkeypatch.setattr(tb, "preflight", lambda *, require_harbor: (True, None))
    monkeypatch.delenv("AR_TERMINALBENCH_RUNTIME_COMMAND", raising=False)
    manifest = {"tasks": [{"task_id": "real", "task_name": "real-task"}]}
    with pytest.raises(RuntimeError, match="runtime_bridge_command_missing"):
        tb.run(manifest, tmp_path, budget_usd=20, mode="harbor")


def test_harbor_task_names_reject_fixture_ids():
    with pytest.raises(ValueError, match="official harbor_task"):
        tb._harbor_task_names(tb.smoke_manifest())


def test_harbor_runner_uses_custom_agent_and_reads_verifier_result(monkeypatch, tmp_path):
    manifest = {"dataset_version": "terminal-bench-2.0", "tasks": [{"task_id": "real", "harbor_task": "task-a"}]}
    monkeypatch.setenv("AR_TERMINALBENCH_RUNTIME_COMMAND", "runtime --instruction {instruction}")

    def fake_run(command, **kwargs):
        assert "--agent" in command and tb.HARBOR_AGENT_IMPORT in command
        result_dir = tmp_path / "harbor-job" / "task-a"
        result_dir.mkdir(parents=True)
        (result_dir / "result.json").write_text(json.dumps({"task_name": "task-a", "verifier_result": {"rewards": {"reward": 1.0}}}))
        return __import__("subprocess").CompletedProcess(command, 0, "ok\n")

    monkeypatch.setattr(tb.subprocess, "run", fake_run)
    result = tb._run_harbor(manifest, tmp_path, budget_usd=20)
    assert result[0].status == "completed"
    assert result[0].verifier_status == "passed"


def test_harbor_parser_prefers_job_trials_without_duplicate_trial_files(tmp_path):
    manifest = {"tasks": [{"task_id": "real", "harbor_task": "task-a"}]}
    job = tmp_path / "job"
    job.mkdir()
    trial = job / "trial"
    trial.mkdir()
    record = {"task_name": "task-a", "trial_name": "task-a__trial", "verifier_result": {"rewards": {"reward": 1.0}}}
    (job / "result.json").write_text(json.dumps({"trial_results": [record]}))
    (trial / "result.json").write_text(json.dumps(record))
    parsed = tb._parse_harbor_results(tmp_path, manifest)
    assert len(parsed) == 1 and parsed[0].verifier_status == "passed"


def test_missing_verifier_is_infrastructure_failure(tmp_path):
    manifest = {"tasks": [{"task_id": "x", "task_name": "x"}]}
    result = tb.run(manifest, tmp_path, budget_usd=1)[0]
    assert result.status == "infra_failed"
    assert result.infra_error_code == "verifier_missing"
