"""Durable provider-quota stop for a single, exclusively owned matrix run."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CODE = "insufficient_user_quota"
MARKER = "provider-quota-pause.json"


class ProviderQuotaPaused(RuntimeError):
    pass


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_running(directory: Path) -> None:
    if (directory / MARKER).exists():
        raise ProviderQuotaPaused(f"{directory.name} paused for provider quota; retain the original run and reconcile billing before resuming")


def is_quota_row(row: dict[str, Any]) -> bool:
    record = row.get("record") or {}
    failures = record.get("model_call_failures") or {}
    if isinstance(failures, dict) and failures.get(CODE, 0):
        return True
    if row.get("provider_quota", {}).get("provider_refused") is True:
        return True
    return any(isinstance(err, str) and err.startswith("DeniedError: " + CODE + ":") for err in (row.get("error"), record.get("error")))


def unreceipted_billing(directory: Path, rows: list[dict[str, Any]]) -> bool:
    recorded = set()
    for row in rows:
        cell = "/".join((row["task_family"], row["role"], row["tier"]))
        ident = (cell, row["eval"], row["candidate"], row["item"], row["repeat"])
        recorded.add(hashlib.sha256(json.dumps(ident).encode()).hexdigest())
    for path in (directory / "item-budgets").glob("*.json"):
        if path.stem not in recorded:
            state = json.loads(path.read_bytes())
            if state.get("pending") or state.get("spent_usd_micros", 0) > 0:
                return True
    return False


class Pause:
    def __init__(self, directory: Path):
        self.directory = directory
        self.first_at: str | None = None

    def trip(self) -> None:
        if self.first_at is None:
            self.first_at = datetime.now(UTC).isoformat()
            self.write()

    def write(self, rows: list[dict[str, Any]] | None = None) -> None:
        marker = {"schema":"capability-provider-pause-v1", "run_id":self.directory.name, "reason":CODE,
                  "active":True, "detected_at":self.first_at, "first_quota_at":self.first_at,
                  "evidence_source":"typed upstream refusal at the matrix gateway", "run_lock_sha256":file_sha(self.directory / "run-lock.json")}
        if rows is not None:
            marker.update(results_sha256=file_sha(self.directory / "results.jsonl"),
                          observed_rows=len(rows), quota_rows=sum(map(is_quota_row, rows)))
        with tempfile.NamedTemporaryFile("w", dir=self.directory, delete=False) as stream:
            try:
                json.dump(marker, stream, separators=(",",":"))
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                Path(stream.name).unlink(missing_ok=True)
                raise
        Path(stream.name).replace(self.directory / MARKER)
