"""Unit tests for SWE Candidate D: single-variable AR_TEST_ENTRY_GUARD verification."""

from __future__ import annotations

from agentruntime_worker.reviewer.rules import ReviewCatalog, enforce, floor
from agentruntime_worker.reviewer.signals import ledger
from agentruntime_worker.workflows.verification import VerificationRecord, is_repo_test_command


def _enforce_done(rec: VerificationRecord) -> dict:
    stage = {"round_seq": 1, "goal": "fix bug", "nodes": [{"id": "d1", "role": "deliver"}]}
    results = {"d1": {"status": "succeeded", "role": "deliver", "result": {"summary": "patched"}}}
    lg = ledger({}, stage, results, [])
    fl = floor(lg, results, completion=1.0)
    dec = enforce({"verdict": "done", "reason": "done", "done": {"deliver_node": "d1"}}, floor=fl, ledger=lg, stage=stage, results=results, catalog=ReviewCatalog(), verification=rec.as_dict())
    return dec.verdict


def test_is_repo_test_command_distinguishes_adhoc_vs_repo_runners() -> None:
    assert is_repo_test_command("shell_run", {"argv": ["pytest", "-q", "tests/test_bug.py"]}) is True
    assert is_repo_test_command("shell_run", {"argv": ["python", "-m", "unittest", "tests.test_bug"]}) is True
    assert is_repo_test_command("shell_run", {"argv": ["./tests/runtests.py", "forms"]}) is True
    assert is_repo_test_command("shell_run", {"argv": ["sh", "-c", "go test ./internal/web/..."]}) is True
    assert is_repo_test_command("shell_run", {"argv": ["python", "-c", "print('ok')"]}) is False
    assert is_repo_test_command("shell_run", {"argv": ["sphinx-build", "-b", "html", "docs", "_build"]}) is False


def test_candidate_d_blocks_adhoc_only_once_when_guard_enabled() -> None:
    rec = VerificationRecord(write_capable=True, require_repo_test_runner=True)
    rec.called("repo_edit", "write_reversible", {"path": "django/forms.py"}, {"ok": True})
    # Ad-hoc inline python script marks ran_after_write, NOT ran_repo_test_runner
    rec.called("shell_run", "write_reversible", {"argv": ["python", "-c", "print('looks good')"]}, {"exit_code": 0})
    assert rec.ran_after_write is True
    assert rec.ran_repo_test_runner is False

    v1 = _enforce_done(rec)
    assert v1["verdict"] == "continue"
    assert v1["reason_code"] == "rule_done_unverified"
    rec.reviewed(v1)

    # Once blocked once, second done attempt succeeds (done_blocked >= 1) OR succeeds after real test runner
    v2 = _enforce_done(rec)
    assert v2["verdict"] == "done"

    # Real repo runner satisfies Candidate D immediately on first done attempt
    rec_real = VerificationRecord(write_capable=True, require_repo_test_runner=True)
    rec_real.called("repo_edit", "write_reversible", {"path": "django/forms.py"}, {"ok": True})
    rec_real.called("shell_run", "write_reversible", {"argv": ["./tests/runtests.py", "auth_tests.test_forms"]}, {"exit_code": 0})
    assert rec_real.ran_repo_test_runner is True
    assert _enforce_done(rec_real)["verdict"] == "done"


def test_default_baseline_preserves_legacy_behavior() -> None:
    rec = VerificationRecord(write_capable=True, require_repo_test_runner=False)
    rec.called("repo_edit", "write_reversible", {"path": "a.py"}, {"ok": True})
    rec.called("shell_run", "write_reversible", {"argv": ["python", "-c", "print('ok')"]}, {"exit_code": 0})
    assert "require_repo_test_runner" not in rec.as_dict()
    assert _enforce_done(rec)["verdict"] == "done"
