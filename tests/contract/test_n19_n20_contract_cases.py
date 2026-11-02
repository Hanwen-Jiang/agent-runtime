"""Candidate acceptance inventory for N-19/N-20; no runtime contract is changed yet."""

from __future__ import annotations

import json
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "n19_n20_contract_cases.json"


def test_n19_n20_inventory_is_unique_and_covers_required_boundaries() -> None:
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(cases) == len({case["id"] for case in cases})
    assert {case["category"] for case in cases} >= {"snapshot", "isolation", "privacy", "latency", "streaming"}
    assert {case["id"] for case in cases} >= {
        "snapshot_has_cursor",
        "delta_starts_after_cursor",
        "reconnect_resume",
        "expired_cursor_requests_snapshot",
        "duplicate_event_is_idempotent",
        "latency_segments",
        "streaming_prerequisite",
    }


def test_streaming_case_records_current_boundary() -> None:
    cases = {case["id"]: case for case in json.loads(FIXTURE.read_text(encoding="utf-8"))}
    assert "StreamGenerate" in cases["streaming_prerequisite"]["expected"]
    assert "sandbox_startup_ms" in cases["cold_start_is_reused"]["expected"]
