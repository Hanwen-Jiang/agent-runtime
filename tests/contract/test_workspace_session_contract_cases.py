"""Candidate acceptance inventory for D-06/P1-C; no runtime contract is changed."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "tests" / "contract" / "fixtures" / "workspace_session_contract_cases.json"
BASELINE = ROOT / "docs" / "plan" / "workspace-session-contract-baseline-2026-11-02.md"


def _data() -> dict:
    return json.loads(CASES.read_text(encoding="utf-8"))


def test_workspace_inventory_is_unique_and_covers_boundaries() -> None:
    data = _data()
    cases = data["cases"]
    ids = [case["id"] for case in cases]
    assert data["schema"] == "workspace_session_contract_cases"
    assert data["version"] == 1
    assert len(ids) == len(set(ids))
    assert {case["area"] for case in cases} >= {
        "workspace",
        "logs",
        "lifecycle",
        "isolation",
        "path_security",
        "privacy",
        "environment_projection",
        "compatibility",
    }
    assert {case["expect"] for case in cases} >= {"allow", "deny", "blocked_external"}
    assert all(case["assertion"].strip() for case in cases)


def test_workspace_inventory_matches_frozen_safety_and_blocker_boundary() -> None:
    ids = {case["id"] for case in _data()["cases"]}
    required = {
        "tree_stays_in_workspace",
        "read_has_size_and_line_limits",
        "log_has_bounded_window",
        "discard_is_idempotent",
        "copy_requires_explicit_target",
        "tenant_session_workspace_isolation",
        "reject_parent_path",
        "reject_symlink_escape_and_host_mount",
        "secret_free_workspace_payloads",
        "env_requires_runtime_service",
        "legacy_sse_event_compatibility",
        "unknown_state_is_not_success",
    }
    assert required <= ids

    baseline = BASELINE.read_text(encoding="utf-8")
    for term in (
        "EnvironmentService",
        "blocked_external",
        "SecretStore",
        "Last-Event-ID",
        "符号链接",
        "不新增环境状态机",
        "S-01/D-08",
    ):
        assert term in baseline
