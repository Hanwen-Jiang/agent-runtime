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


def test_harbor_mode_never_reports_fixture_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(tb, "preflight", lambda *, require_harbor: (True, None))
    with pytest.raises(RuntimeError, match="harbor_bridge_not_implemented"):
        tb.run(tb.smoke_manifest(), tmp_path, budget_usd=20, mode="harbor")


def test_missing_verifier_is_infrastructure_failure(tmp_path):
    manifest = {"tasks": [{"task_id": "x", "task_name": "x"}]}
    result = tb.run(manifest, tmp_path, budget_usd=1)[0]
    assert result.status == "infra_failed"
    assert result.infra_error_code == "verifier_missing"
