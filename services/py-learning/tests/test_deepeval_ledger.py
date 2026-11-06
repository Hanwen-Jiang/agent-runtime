"""Authoritative cost/Registry evidence, including real Postgres tenant isolation."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentruntime_learning.evals import deepeval_ledger as ledger, deliverables as dv
from agentruntime_learning.benchmarks import swebench as sb
from test_deepeval_acceptance import collected
from test_pipeline_costs import pg_cluster  # noqa: F401 — reuse the real ephemeral Postgres fixture


def attempt(task: str, *, rid: str = "attempt-one", cost: int | None = 80000, **changes) -> dict:
    return {"reservation_id": rid, "task_id": task, "node_id": "plan", "model_profile_id": "example/model@1",
            "actual_usd_micros": cost, "registry_scope": "platform", "registry_revision": "v2",
            "registry_document_sha256": "e" * 64, **changes}


def subject(out: Path):
    case, _ = collected(out)
    records = sb.read_records(out)
    return case, records[case.instance_id]["task_id"]


def judged(out: Path, cid: str) -> None:
    directory = out / "scores"; directory.mkdir(exist_ok=True)
    (directory / "summary.json").write_text(json.dumps({
        "selected": 1, "scored": 1, "passed_all": 1, "pass_rate": 1,
        "metrics": {"honesty": {"mean": 1, "passed": 1, "scored": 1, "errors": 0}},
    }))
    (directory / "judgments.jsonl").write_text(json.dumps({
        "case_id": cid, "judge_pinned_ok": True, "usage": {"cost_usd_micros": 3000},
    }) + "\n")
    (directory / "budget-state.json").write_text(json.dumps({
        "limit_usd_micros": 1000000, "spent_usd_micros": 3000, "pending": False,
    }))


async def test_authoritative_attempts_override_only_the_cost_conclusion(tmp_path):
    case, task = subject(tmp_path)
    judged(tmp_path, case.instance_id)
    # Perfect scores and nonempty SSE evidence still do not prove true costs.
    before = dv.report(tmp_path)
    assert before["costs"]["subject_usd"] == 0.05
    assert before["costs"]["authoritative_subject_usd"] is None
    assert before["costs"]["total_usd"] is None and not before["acceptance_ready"]
    record_bytes = (tmp_path / "records" / f"{case.instance_id}.json").read_bytes()

    async def collect(tid, tenant):
        assert tid == task and tenant == "evals"
        first = attempt(task, cost=30000)
        return [first, first, attempt(task, rid="retry", cost=50000)]

    evidence = await ledger.audit(tmp_path, "evals", collector=collect)
    after = dv.report(tmp_path)
    assert evidence["assessment"]["attempts"] == 2, "duplicate retrieval is idempotent"
    assert after["costs"]["subject_usd"] == 0.05, "old reported amount remains an explicit comparison"
    assert after["costs"]["authoritative_subject_usd"] == 0.08
    assert after["costs"]["total_usd"] == pytest.approx(0.083)
    assert after["subject_ledger"]["reported_cost_comparison"][case.instance_id]["ledger_minus_reported_usd"] == 0.03
    assert after["acceptance_ready"]
    assert (tmp_path / "records" / f"{case.instance_id}.json").read_bytes() == record_bytes
    again = await ledger.audit(tmp_path, "evals", collector=collect)
    assert again["evidence_sha256"] == evidence["evidence_sha256"]
    assert len(list((tmp_path / "ledger-evidence").glob("audit-*.json"))) == 1


@pytest.mark.parametrize("changes", [
    {"cost": None}, {"model_profile_id": "other/model@1"}, {"registry_revision": "v3"},
    {"registry_scope": "_platform"}, {"registry_document_sha256": "a" * 64}, {"task_id": "some-other-task"},
])
async def test_unknown_cost_or_wrong_attempt_identity_never_becomes_ready(tmp_path, changes):
    case, task = subject(tmp_path)
    judged(tmp_path, case.instance_id)
    row = attempt(task)
    row.update({"actual_usd_micros" if k == "cost" else k: v for k, v in changes.items()})
    async def collect(*args):
        return [row]
    await ledger.audit(tmp_path, "evals", collector=collect)
    report = dv.report(tmp_path)
    assert not report["acceptance_ready"] and not report["subject_ledger"]["valid"]
    if "cost" in changes or "task_id" in changes:
        assert report["costs"]["total_usd"] is None


async def test_pending_ledger_cannot_spend_on_frozen_scoring(tmp_path):
    _, task = subject(tmp_path)
    async def collect(*args):
        return [attempt(task, cost=None)]
    await ledger.audit(tmp_path, "evals", collector=collect)
    with pytest.raises(ValueError, match="model_usage"):
        dv.score(tmp_path, gateway=object(), judge_budget_usd=1)
    assert not (tmp_path / "scores").exists()


async def test_missing_and_unreadable_costs_are_unknown_and_errors_are_redacted(tmp_path):
    _, task = subject(tmp_path)
    async def collect(*args):
        raise RuntimeError("postgres://user:DO-NOT-RECORD@host")
    doc = await ledger.audit(tmp_path, "evals", collector=collect)
    assert doc["assessment"]["unknown_task_ids"] == [task]
    assert not doc["assessment"]["valid"]
    assert "DO-NOT-RECORD" not in (tmp_path / "ledger-evidence/latest.json").read_text()


async def test_stale_record_binding_or_edited_audit_summary_is_not_trusted(tmp_path):
    case, task = subject(tmp_path)
    async def collect(*args):
        return [attempt(task, cost=None)]
    await ledger.audit(tmp_path, "evals", collector=collect)
    path = tmp_path / "ledger-evidence/latest.json"
    doc = json.loads(path.read_text()); doc["assessment"]["valid"] = True
    path.write_text(json.dumps(doc))
    assert not ledger.status(tmp_path, *ledger._inputs(tmp_path))["valid"], "recompute from attempts"
    rec = tmp_path / "records" / f"{case.instance_id}.json"
    row = json.loads(rec.read_text()); row["cost_usd"] = 0
    rec.write_text(json.dumps(row))
    assert ledger.status(tmp_path, *ledger._inputs(tmp_path))["problems"] == ["ledger_evidence_invalid_or_stale"]


async def test_history_audit_writes_to_new_evidence_directory_only(tmp_path):
    original, destination = tmp_path / "original", tmp_path / "audit"
    case, task = subject(original)
    manifest = json.loads((original / "run.json").read_text()); manifest.pop("identity")
    (original / "run.json").write_text(json.dumps(manifest))
    before = {p: p.read_bytes() for p in original.rglob("*") if p.is_file()}
    async def collect(*args):
        return [attempt(task)]
    result = await ledger.audit(original, "evals", evidence_dir=destination, collector=collect)
    assert result["assessment"]["authoritative_subject_usd"] == .08
    assert not result["assessment"]["valid"], "old rows cannot gain a fabricated frozen deployment"
    assert before == {p: p.read_bytes() for p in original.rglob("*") if p.is_file()}


async def test_audit_detects_complete_task_set_and_refuses_concurrent_record_changes(tmp_path):
    case, task = subject(tmp_path)
    record_path = tmp_path / "records" / f"{case.instance_id}.json"
    async def changed(*args):
        row = json.loads(record_path.read_text()); row["task_id"] = "replacement-task"
        record_path.write_text(json.dumps(row))
        return [attempt(task)]
    with pytest.raises(ValueError, match="changed"):
        await ledger.audit(tmp_path, "evals", collector=changed)
    assert not (tmp_path / "ledger-evidence/latest.json").exists()
    run, records = ledger._inputs(tmp_path)
    tasks = {"replacement-task": {"rows": [attempt("replacement-task")]},
             "unselected-task": {"rows": [attempt("unselected-task", rid="unrelated")]}}
    result = ledger.assess(run, records, tasks)
    assert not result["valid"] and not result["cost_known"]
    assert "ledger_task_set_mismatch" in result["problems"]
    run["instance_ids"].append("duplicate-case")
    records["duplicate-case"] = dict(records[case.instance_id])
    result = ledger.assess(run, records, {"replacement-task": tasks["replacement-task"]})
    assert "task_reused_by_cases" in result["problems"] and not result["cost_known"]


def test_invalid_tenant_in_saved_evidence_is_not_an_exception_from_report(tmp_path):
    subject(tmp_path)
    directory = tmp_path / "ledger-evidence";directory.mkdir()
    (directory / "latest.json").write_text(json.dumps({"tenant_id": "../other"}))
    report = dv.report(tmp_path)
    assert not report["acceptance_ready"] and report["subject_ledger"]["problems"] == ["ledger_evidence_invalid_or_stale"]


async def test_real_postgres_rls_counts_only_the_bound_tenant_and_never_updates_reservations(tmp_path, pg_cluster, monkeypatch):
    from agentruntime_core.adapters import pgtest
    from agentruntime_core.adapters.postgres import PostgresRelationalStoreProvider
    from agentruntime_core.providers.relational_store import Migration
    from agentruntime_core.tenancy import TenantContext

    case, task = subject(tmp_path)
    db = pg_cluster.new_database("deep_ledger")
    dsn = pg_cluster.dsn(pgtest.APP_ROLE, db)
    provider = await PostgresRelationalStoreProvider.connect(dsn)
    try:
        await provider.migrate([Migration(version=301, name="eval_ledger", statements=(
            "CREATE TABLE model_usage (reservation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, node_id TEXT NOT NULL, "
            "model_profile_id TEXT NOT NULL, actual_usd_micros BIGINT, registry_scope TEXT, registry_revision TEXT, registry_document_sha256 TEXT)",
        ))])
        for tenant, cost in (("evals", 80000), ("unrelated", 9000000)):
            row = attempt(task, rid="attempt-" + tenant, cost=cost)
            async with provider.bind(TenantContext(tenant_id=tenant)).transaction() as tx:
                await tx.execute("INSERT INTO model_usage (" + ",".join(ledger.FIELDS) + ") VALUES (" +
                                 ",".join("$" + str(i) for i in range(1, len(ledger.FIELDS) + 1)) + ")",
                                 [row[k] for k in ledger.FIELDS])
        monkeypatch.setenv("AR_RELATIONAL_DSN", dsn)
        result = await ledger.audit(tmp_path, "evals")
        assert result["assessment"]["valid"] and result["assessment"]["authoritative_subject_usd"] == .08
        assert result["assessment"]["attempts"] == 1
        judged(tmp_path, case.instance_id)
        assert dv.report(tmp_path)["acceptance_ready"]
        cli = subprocess.run([sys.executable, "-m", "agentruntime_learning.evals.deepeval_ledger", "audit",
                              "--out", str(tmp_path), "--tenant", "evals"], capture_output=True, text=True, timeout=30)
        assert cli.returncode == 0, cli.stderr
        assert json.loads(cli.stdout)["authoritative_subject_usd"] == .08
        async with provider.bind(TenantContext(tenant_id="evals")).transaction(read_only=True) as tx:
            rows = await tx.query("SELECT actual_usd_micros FROM model_usage")
        assert [r["actual_usd_micros"] for r in rows] == [80000], "audit did not rewrite costs"
        denied = await ledger.audit(tmp_path, "tenant_without_records", evidence_dir=tmp_path / "other-audit")
        assert not denied["assessment"]["cost_known"]
    finally:
        await provider.aclose()
        pg_cluster.drop_database(db)
