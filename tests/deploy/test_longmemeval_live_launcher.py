"""The live launcher refuses drift and budget uncertainty before any writes."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("memory_launch", Path(__file__).resolve().parents[2] / "scripts/run_longmemeval_live.py")
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


def row(**kwargs):
    return {"model_profile_id": launch.MODEL, "method": "embed", "task_id": "", "actual_usd_micros": 100,
            "reserved_usd_micros": 200, **kwargs}


def test_budget_uses_every_attempt_and_keeps_prior_paid_work():
    money = launch.check_money([row(), row()], before_question=True)
    assert money["actual_usd"] == .0002 and money["total_including_history_usd"] == .015491
    assert money["conservative_reserved_usd"] == .0004
    with pytest.raises(launch.PreflightError, match="pending"):
        launch.check_money([row(actual_usd_micros=None)], before_question=True)
    with pytest.raises(launch.PreflightError, match="admission"):
        launch.check_money([row(reserved_usd_micros=launch.ADMIT_MICROS)], before_question=True)
    with pytest.raises(launch.PreflightError, match="unrelated"):
        launch.check_money([row(task_id="other-task")], before_question=True)


def registry():
    policy={"model_allowlist":[launch.MODEL],"model_budget":{"daily_usd":launch.CAP_MICROS/1e6,"monthly_usd":launch.CAP_MICROS/1e6}}
    profile={"id":launch.MODEL,"cost":{"in_per_m":.13}}
    db={"missing_ledger_columns":[],"profile":[{"version":"v1","document_sha256":"a"*64,"document":profile}],
        "tenant_policy":[{"version":"v1","document_sha256":"b"*64,"document":policy}],"model_usage":[]}
    file={"profiles":[profile],"registry_revisions":{launch.MODEL:{"scope":"platform","version":"v1","sha256":"a"*64}}}
    policies={"tenants":{launch.TENANT:policy}}
    return db,file,policies


def test_registry_actual_export_and_policy_must_all_agree():
    db,file,policies=registry()
    assert launch.validate_registry(db,file,policies)["profile"]["version"]=="v1"
    file["registry_revisions"][launch.MODEL]["version"]="v2"
    with pytest.raises(launch.PreflightError,match="Registry"):
        launch.validate_registry(db,file,policies)
    db,file,policies=registry()
    db["model_usage"]=[{"registry_scope":"platform","registry_revision":"v2","registry_document_sha256":"a"*64}]
    with pytest.raises(launch.PreflightError,match="real attempt"):
        launch.validate_registry(db,file,policies)
    db,file,policies=registry()
    policies["tenants"][launch.TENANT]["model_budget"]["daily_usd"]=20
    with pytest.raises(launch.PreflightError,match="hard budget"):
        launch.validate_registry(db,file,policies)


def test_new_fixed_run_keeps_settled_prior_cost_and_rejects_new_unattributed_calls(tmp_path):
    old = row(reservation_id="old", actual_usd_micros=60847)
    db = {"tenant": launch.TENANT, "model_usage": [old]}
    (tmp_path / "ledger-after.json").write_text(json.dumps(db))
    (tmp_path / "stop-cleanup-recovery.json").write_text(json.dumps({
        "complete": True, "original_process_terminated": True, "remaining_ids": [],
        "queue_count": 0, "pending_attempts": 0, "actual_usd": .060847,
    }))
    proof = launch.prior_experiment(tmp_path, db, fresh=True)
    assert proof["actual_usd_micros"] == 60847
    assert launch.check_money(db["model_usage"], before_question=True)["total_including_history_usd"] == .076138
    with pytest.raises(launch.PreflightError, match="charged"):
        launch.prior_experiment(None, db, fresh=True)
    new = {"tenant": launch.TENANT, "model_usage": [old, row(reservation_id="new")]}
    with pytest.raises(launch.PreflightError, match="matches"):
        launch.prior_experiment(tmp_path, new, fresh=True)
    assert launch.prior_experiment(tmp_path, new, fresh=False) == proof
    old["actual_usd_micros"] = 0
    with pytest.raises(launch.PreflightError, match="matches"):
        launch.prior_experiment(tmp_path, db, fresh=False)
