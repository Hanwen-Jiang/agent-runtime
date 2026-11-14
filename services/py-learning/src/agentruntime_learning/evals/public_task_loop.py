"""Read completed task-loop reports and expose aggregate evaluation evidence only.

Supported producer locations below the read-only evaluation mount are reports/,
task-loop/, reviewer/, task-loop-*/ and reviewer-*/ (at most four levels), plus
src/<git revision>/agent-runtime/services/py-learning/.eval-reports/. We never
walk arbitrary source trees, follow symlinks or expose cases, prompts or paths.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_BYTES = 4 * 1024 * 1024
MAX_FILES = 1024
MAX_DIRECTORIES = 512
MAX_ENTRIES = 8192
MAX_TOTAL_BYTES = 64 * 1024 * 1024
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
EVAL_SETS = {"task-loop", "reviewer-regressions-dev"}
REPORT = re.compile(r"^(?:task-loop|reviewer-regressions-dev)--[A-Za-z0-9_.-]{1,200}\.json$")
REVISION = re.compile(r"^[0-9a-f]{7,40}$")
SKIP = {"private", "secrets", "usage", "ledgers", "source", "node_modules", "venv", "data", "records"}
ROLES = {"plan", "judge", "route", "act", "transform", "deliver", "sub_agent"}
PROVENANCE_FIELDS = {"protocol", "variant", "comparison_id", "code_revision", "code_sha256", "scoring_version", "reviewer_profile",
                     "cohort_id", "deliverable_judge", "selection_sha256"}


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _number(value: Any, *, ratio: bool = False) -> int | float | None:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 2**53 - 1:
        return None
    return value if not ratio or value <= 1 else None


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else None


def _text(value: Any, limit: int = 256) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 for c in value) else None


def public_models(value: Any) -> dict[str, dict[str, int]]:
    """Only the documented role/profile/count model provenance, never arbitrary nested metadata."""
    result = {}
    for role, profiles in _mapping(value).items():
        if role not in ROLES:
            continue
        clean = {profile: count for profile, count in list(_mapping(profiles).items())[:64]
                 if _text(profile) and _count(count) is not None}
        if clean:
            result[role] = clean
    return result


def _timestamp(value: Any) -> str | None:
    if not _text(value, 64):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone(UTC).isoformat() if stamp.tzinfo else None
    except (ValueError, OverflowError):
        return None


def run_identity(row: dict[str, Any]) -> tuple[str, ...]:
    if row.get("run_id"):
        return ("id", row["run_id"])
    return ("legacy", row.get("eval_set_name") or "task-loop", str(row.get("subject", "")), str(row.get("split", "")),
            _timestamp(row.get("created_at")) or str(row.get("created_at", "")))


def merge_task_loop_runs(archive: list[dict], fresh: list[dict]) -> list[dict]:
    """Prefer raw evidence while matching pre-run_id archives by subject/split/time."""
    result = list(archive)
    for row in fresh:
        identity = run_identity(row)
        matches = [i for i, prior in enumerate(result) if run_identity(prior) == identity or
                   (not prior.get("run_id") and prior.get("created_at") and
                    (prior.get("eval_set_name") or "task-loop") == (row.get("eval_set_name") or "task-loop") and
                    all(prior.get(k) == row.get(k) for k in ("subject", "split")) and
                    _timestamp(prior.get("created_at")) == _timestamp(row.get("created_at")))]
        if matches:
            # Retain useful human labels from an existing archive, but replace all measurements.
            prior = result[matches[0]]
            row = {**row, **{k: prior[k] for k in ("label_zh", "label_en", "group") if prior.get(k)}}
            result = [r for i, r in enumerate(result) if i not in matches]
        result.append(row)
    return sorted(result, key=lambda row: _timestamp(row.get("created_at")) or "")


def project_report(data: Any, digest: str) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("metrics"), dict):
        raise ValueError("invalid task-loop report")
    subject, split = _text(data.get("subject"), 96), data.get("split")
    created_at = _timestamp(data.get("created_at"))
    evaluation = _mapping(data.get("eval_set"))
    if not subject or split not in ("dev", "holdout") or created_at is None or evaluation.get("name") not in EVAL_SETS:
        raise ValueError("invalid report identity")
    metrics = data["metrics"]
    planner, reviewer, rounds, cost = (_mapping(metrics.get(key)) for key in ("planner", "reviewer", "rounds", "cost"))
    models = public_models(data.get("models"))
    # S-09 subjects use a rule Reviewer; those results must not become model scores.
    model_reviewer = subject == "worker"
    fallback = _mapping(metrics.get("fallbacks"))
    rate, limit = _number(fallback.get("rate"), ratio=True), _number(data.get("max_fallback_rate"), ratio=True)
    valid = data.get("valid") if type(data.get("valid")) is bool else None
    if rate is not None and limit is not None and rate > limit:
        valid = False
    counts = [_count(part.get("decisions")) for part in (planner, reviewer, rounds)]
    decisions = sum(counts) if all(value is not None for value in counts) else None
    fallback_counts = [_count(fallback.get(k)) for k in ("planner", "reviewer")]
    fallbacks = sum(fallback_counts) if all(value is not None for value in fallback_counts) else None
    run_id = _text(data.get("run_id"))
    # Legacy reports lack a run ID. This stable identity is independent of file location/content.
    if run_id is None:
        key = json.dumps([evaluation["name"], subject, split, created_at], separators=(",", ":")).encode()
        run_id = "task-loop-" + hashlib.sha256(key).hexdigest()[:24]
    version = _text(evaluation.get("version"), 128)
    model = ", ".join(sorted(models.get("judge", {}))) or None
    row = {
        "run_id": run_id, "group": evaluation["name"], "eval_set_name": evaluation["name"], "subject": subject, "split": split, "created_at": created_at,
        "label_zh": model or subject, "label_en": model or subject, "model": model, "models": models,
        "eval_set_version": version, "dataset_hash": version, "source_sha256": digest, "source_kind": "task-loop-report",
        "triage": _number(planner.get("triage_accuracy"), ratio=True), "shape": _number(planner.get("shape_match_rate"), ratio=True),
        "reviewer": _number(reviewer.get("verdict_accuracy"), ratio=True) if model_reviewer else None,
        "rounds": _number(rounds.get("verdict_accuracy"), ratio=True) if model_reviewer else None,
        "reviewer_samples": _count(reviewer.get("decisions")), "round_samples": _count(rounds.get("decisions")),
        "reviewer_schema_pass_rate": _number(reviewer.get("schema_pass_rate"), ratio=True),
        "stuck": _count(metrics.get("stuck_retry_errors")), "total_usd": _number(cost.get("total_usd")),
        "per_case_usd": _number(cost.get("per_case_usd")), "task_total": _count(planner.get("decisions")),
        "task_pass": None, "fallbacks": fallbacks, "fallback_rate": rate, "max_fallback_rate": limit,
        "reviewer_fallbacks": _count(fallback.get("reviewer")), "decisions": decisions, "valid": valid,
        "structured_failures": _count(_mapping(data.get("model_call_failure_codes")).get("bad_structured_output")),
        "labels_confirmed": _count(_mapping(data.get("labels")).get("confirmed")),
        "labels_draft": _count(_mapping(data.get("labels")).get("draft")),
        "repeat": _count(data.get("repeat")),
    }
    # These are counts only. Never project a case ID, input, expectation or output, even for dev.
    cases = data.get("cases")
    if split == "dev" and isinstance(cases, list):
        tasks = [c for c in cases if isinstance(c, dict) and c.get("kind") == "task"]
        if tasks:
            row["task_total"] = len(tasks)
            row["task_pass"] = sum(c.get("passed") is True for c in tasks)
    for field in PROVENANCE_FIELDS:
        if value := _text(data.get(field), 128):
            row[field] = value
    # The comparison producer calls its frozen code hash source_sha256; the public
    # source_sha256 above always identifies the report bytes, so keep both meanings.
    if "code_sha256" not in row and isinstance(data.get("source_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", data["source_sha256"]):
        row["code_sha256"] = data["source_sha256"]
    return row


class PublicTaskLoopSource:
    def __init__(self, root: Path, *, ttl_s: float = 5.0) -> None:
        self.root, self.ttl_s = Path(root), ttl_s
        self._lock = threading.Lock()
        self._last_read = float("-inf")
        self._cached: dict | None = None
        self._good: dict[str, tuple[int, dict]] = {}

    def _open_directory(self, relative: tuple[str, ...] = ()) -> int:
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in relative:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _scan(self, errors: set[str]) -> list[tuple[str, ...]]:
        files: list[tuple[str, ...]] = []
        directories = entries_seen = 0

        def visit(parts: tuple[str, ...], depth: int = 0, *, root: bool = False, revisions: bool = False) -> None:
            nonlocal directories, entries_seen
            directories += 1
            if directories > MAX_DIRECTORIES:
                raise ValueError("scan limit")
            fd = self._open_directory(parts)
            try:
                with os.scandir(fd) as entries:
                    found = []
                    for entry in entries:
                        entries_seen += 1
                        if entries_seen > MAX_ENTRIES:
                            raise ValueError("scan limit")
                        found.append((entry.name, entry.is_dir(follow_symlinks=False), entry.is_symlink()))
                for name, is_dir, symlink in sorted(found):
                    path = (*parts, name)
                    if REPORT.fullmatch(name):
                        if symlink or is_dir:
                            errors.add("task_loop_unsafe_path")
                            continue
                        files.append(path)
                        if len(files) > MAX_FILES:
                            raise ValueError("report limit")
                        continue
                    if revisions:
                        selected = bool(REVISION.fullmatch(name))
                    elif root:
                        selected = name in {"reports", "task-loop", "reviewer", "src", ".eval-reports"} or name.startswith(("task-loop-", "reviewer-", "s10-"))
                    else:
                        selected = depth < 4 and bool(NAME.fullmatch(name)) and name not in SKIP
                    if not selected:
                        continue
                    if symlink:
                        errors.add("task_loop_unsafe_path")
                    elif is_dir:
                        try:
                            if revisions:
                                visit((*path, "agent-runtime", "services", "py-learning", ".eval-reports"))
                            else:
                                visit(path, depth + 1, revisions=root and name == "src")
                        except FileNotFoundError:
                            continue  # Most source snapshots have no report directory.
            finally:
                os.close(fd)
        visit((), root=True)
        return files

    def _read(self, path: tuple[str, ...]) -> tuple[bytes, int]:
        fd = self._open_directory(path[:-1])
        try:
            # NONBLOCK prevents a named pipe with a report-looking name from blocking HTTP.
            file_fd = os.open(path[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                    raise ValueError("invalid report file")
                raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("report too large")
                return raw, info.st_mtime_ns
        finally:
            os.close(fd)

    def get(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if self._cached is not None and now - self._last_read < self.ttl_s:
                return self._cached
            errors: set[str] = set()
            try:
                paths = self._scan(errors)
                seen = {"/".join(path) for path in paths}
                total = 0
                for path in paths:
                    key = "/".join(path)
                    try:
                        raw, modified = self._read(path)
                        total += len(raw)
                        if total > MAX_TOTAL_BYTES or key not in self._good and len(self._good) >= MAX_FILES:
                            errors.add("task_loop_scan_limit")
                            break
                        row = project_report(json.loads(raw), hashlib.sha256(raw).hexdigest())
                        self._good[key] = (modified, row)
                    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                        errors.add("task_loop_invalid")
                if set(self._good) - seen:
                    errors.add("task_loop_missing")
            except (OSError, ValueError):
                errors.add("task_loop_unavailable")
            rows = {}
            for _, row in sorted(self._good.values(), key=lambda pair: pair[0]):
                rows[run_identity(row)] = row
            self._last_read = now
            self._cached = {"items": sorted(rows.values(), key=lambda row: row["created_at"]), "source": "filesystem",
                            "stale": bool(errors), "status": "stale" if errors and rows else "unavailable" if errors else "ready",
                            "checked_at": datetime.now(UTC).isoformat(), "errors": sorted(errors)}
            return self._cached
