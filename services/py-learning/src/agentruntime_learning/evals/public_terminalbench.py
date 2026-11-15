"""Read-only terminal Terminal-Bench summaries with explicit grading coverage.

Producer contract: ``tb20-*/public-summary.json``, schema
``terminalbench-summary-v1``. Raw runner reports are preserved separately.
``selected_ids`` and ``cases`` must cover the identical cohort. Each case has
``instance_id``, ``outcome`` (resolved/unresolved/not_run/infra_error/grading_error)
and, if executed, ``source_attempt`` with run_id, runtime_commit, sandbox_commit,
collector_commit (40 hex each) and carried (bool). Graded cases also have a finite
numeric reward. Counts and rates are computed here, never copied from a report.

Cost fields: cost_scope=cohort_tasks|cohort|campaign, cost_known, known_cost_usd,
pending_reserved_usd, actual_cost_usd. Pending reservations are NOT charges.
Optional campaign_cost uses the same four numeric/boolean fields. Public output
contains only allowlisted identities, numeric facts and revision hashes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_BYTES = 2 * 1024 * 1024
MAX_DIRECTORIES = 128
MAX_ENTRIES = 8192
MAX_CASES = 1000
DIRECTORY = re.compile(r"^tb20-[A-Za-z0-9][A-Za-z0-9_.-]{0,90}$")
IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,191}$")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@/-]{0,255}$")
OUTCOMES = frozenset({"resolved", "unresolved", "not_run", "infra_error", "grading_error"})
SYSTEM_PROTOCOL = "terminalbench-official-same-firecracker-v1"
PROTOCOLS = frozenset({"runtime-workdir-official-tests-v2-binary", SYSTEM_PROTOCOL})


def _identifier(value: Any, pattern: re.Pattern[str] = IDENTITY) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("invalid public identity")
    return value


def _hash(value: Any, length: int = 64) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{%d}" % length, value):
        raise ValueError("invalid evidence digest")
    return value


def _number(value: Any) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid nonnegative finite number")
    return float(value)


def _cost(raw: dict[str, Any]) -> dict[str, Any]:
    known = _number(raw.get("known_cost_usd"))
    pending = _number(raw.get("pending_reserved_usd"))
    flag = raw.get("cost_known")
    if type(flag) is not bool:
        raise ValueError("cost completeness must be explicit")
    actual = raw.get("actual_cost_usd")
    if flag:
        actual = _number(actual)
        if pending != 0 or abs(actual - known) > 0.000001:
            raise ValueError("complete cost conflicts with ledger")
    elif actual is not None:
        raise ValueError("unknown complete cost cannot be reported as exact")
    if pending > 0 and (flag or actual is not None):
        raise ValueError("pending reservations cannot be settled cost")
    return {"cost_known": flag, "known_cost_usd": known,
            "pending_reserved_usd": pending, "actual_cost_usd": actual}


def _attempt(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or type(raw.get("carried")) is not bool:
        raise ValueError("missing case provenance")
    out = {"run_id": _identifier(raw.get("run_id")), "carried": raw["carried"]}
    for key in ("runtime_commit", "sandbox_commit", "collector_commit"):
        out[key] = _hash(raw.get(key), 40)
    for key in ("collector_script_sha256", "patch_sha256", "grading_sha256"):
        if key in raw:
            out[key] = _hash(raw[key])
    if "collector_method" in raw:
        out["collector_method"] = _identifier(raw["collector_method"])
    return out


def _resources(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("invalid resource disclosure")
    result = {}
    for key in ("vcpus", "memory_mb", "private_root_mb", "workspace_scratch_mb", "official_storage_mb"):
        if key in raw:
            value = _number(raw[key])
            if value <= 0:
                raise ValueError("resource capacity must be positive")
            result[key] = value
    if "strict_official_disk_equivalence" in raw:
        if type(raw["strict_official_disk_equivalence"]) is not bool:
            raise ValueError("resource equivalence must be explicit")
        result["strict_official_disk_equivalence"] = raw["strict_official_disk_equivalence"]
    return result


def project_terminalbench_summary(raw: Any, source_sha256: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema") != "terminalbench-summary-v1" or raw.get("benchmark") != "terminal-bench":
        raise ValueError("unsupported Terminal-Bench public summary")
    if raw.get("protocol") not in PROTOCOLS or raw.get("dataset_version") != "2.0":
        raise ValueError("unsupported benchmark execution protocol")
    rid = _identifier(raw.get("run_id"))
    model = _identifier(raw.get("model_name"), MODEL)
    timestamp = raw.get("completed_at")
    if not isinstance(timestamp, str):
        raise ValueError("missing completion timestamp")
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("completion timestamp needs timezone")
    timestamp = moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    started_at = raw.get("started_at")
    if started_at is not None:
        if not isinstance(started_at, str):
            raise ValueError("invalid start timestamp")
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if started.tzinfo is None or started > moment:
            raise ValueError("invalid start timestamp")
        started_at = started.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    selected = raw.get("selected_ids")
    rows = raw.get("cases")
    if not isinstance(selected, list) or not 0 < len(selected) <= MAX_CASES or not isinstance(rows, list) or len(rows) != len(selected):
        raise ValueError("missing complete cohort coverage")
    ids = [_identifier(i) for i in selected]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate selected identity")
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid case")
        iid = _identifier(row.get("instance_id"))
        outcome = row.get("outcome")
        if iid in by_id or iid not in ids or outcome not in OUTCOMES:
            raise ValueError("invalid case outcome or identity")
        item: dict[str, Any] = {"instance_id": iid, "outcome": outcome}
        if outcome != "not_run":
            item["source_attempt"] = _attempt(row.get("source_attempt"))
            if "cost_known" in row:
                item.update(_cost(row))
        if outcome in ("resolved", "unresolved"):
            reward = _number(row.get("reward"))
            if reward > 1 or (reward >= 1) != (outcome == "resolved"):
                raise ValueError("grading outcome conflicts with actual reward")
            item["reward"] = reward
            if raw["protocol"] == SYSTEM_PROTOCOL:
                for key in ("same_vm_verified", "official_initial_state_verified"):
                    if row.get(key) is not True:
                        raise ValueError("system grading requires verified machine and initial state")
                    item[key] = True
                if row.get("guest_profile") != "benchmark-system":
                    raise ValueError("system grading requires benchmark-system profile")
                item["guest_profile"] = "benchmark-system"
                item["test_payload_sha256"] = _hash(row.get("test_payload_sha256"))
                for key in ("agent_timeout_s", "verifier_timeout_s"):
                    value = row.get(key)
                    if type(value) is not int or not 0 < value <= 86400:
                        raise ValueError("official task timeouts must be recorded")
                    item[key] = value
        elif row.get("reward") is not None:
            raise ValueError("ungraded case cannot have a reward")
        if "resource_actual" in row:
            item["resource_actual"] = _resources(row["resource_actual"])
        for key in ("controller_bundle_manifest_sha256", "deadline_audit_sha256"):
            if key in row:
                item[key] = _hash(row[key])
        for key in ("official_test_count", "official_test_passed", "tools_at_or_after_deadline_count"):
            if key in row:
                if type(row[key]) is not int or row[key] < 0:
                    raise ValueError("invalid test or deadline count")
                item[key] = row[key]
        if item.get("official_test_passed", 0) > item.get("official_test_count", 0):
            raise ValueError("passed tests exceed executed tests")
        by_id[iid] = item
    counts = Counter(row["outcome"] for row in by_id.values())
    graded = counts["resolved"] + counts["unresolved"]
    complete = graded == len(ids)
    if raw.get("status") != ("completed" if complete else "partial"):
        raise ValueError("terminal summary completeness conflicts with cases")
    cost = _cost(raw)
    scope = raw.get("cost_scope")
    if scope not in ("cohort_tasks", "cohort", "campaign"):
        raise ValueError("unknown billing attribution")
    rate = counts["resolved"] / graded if graded else None
    scores: dict[str, float] = {"resolved": counts["resolved"]}
    if rate is not None:
        scores["resolved_rate"] = rate
    digest = _hash(source_sha256) if source_sha256 is not None else hashlib.sha256(json.dumps(raw, sort_keys=True, allow_nan=False).encode()).hexdigest()
    result = {
        "run_id": rid, "suite": "terminal-bench", "benchmark": "terminal-bench",
        "subject": "agent-runtime" if model.startswith("agent-runtime") else model,
        "model": model, "model_name": model, "split": "2.0", "evaluation_type": "runtime",
        "source": "terminalbench_artifacts", "source_sha256": digest,
        "started_at": started_at, "created_at": timestamp, "updated_at": timestamp, "completed_at": timestamp,
        "selected": len(ids), "planned": len(ids), "started": len(ids) - counts["not_run"],
        "done": len(ids) - counts["not_run"], "graded_count": graded, "sample_count": graded,
        "resolved": counts["resolved"], "unresolved": counts["unresolved"], "not_run": counts["not_run"],
        "infra_failed": counts["infra_error"], "grading_errors": counts["grading_error"],
        "errors": counts["infra_error"] + counts["grading_error"], "coverage": graded / len(ids),
        "rate_denominator": graded, "denominator_kind": "officially_graded", "rate": rate, "scores": scores,
        "complete": complete, "graded_complete": complete, "finished": True, "status": "finished" if complete else "partial",
        "outcomes": dict(counts), "cases": [by_id[i] for i in ids],
        "carried_count": sum(row.get("source_attempt", {}).get("carried", False) and row["outcome"] in ("resolved", "unresolved") for row in by_id.values()),
        "cohort_sha256": hashlib.sha256(json.dumps(sorted(ids), separators=(",", ":")).encode()).hexdigest(),
        "dataset_version": _identifier(raw.get("dataset_version")), "dataset_sha256": _hash(raw.get("dataset_sha256")),
        "protocol": raw["protocol"], "cost_scope": "cohort_tasks" if scope == "cohort" else scope,
        "cost_usd": cost["actual_cost_usd"], **cost,
    }
    if "run_source_sha256" in raw:
        result["run_source_sha256"] = _hash(raw["run_source_sha256"])
    if "resource_actual" in raw:
        result["resource_actual"] = _resources(raw["resource_actual"])
    for key in ("limitations_zh", "limitations_en"):
        if key in raw:
            if not isinstance(raw[key], str) or not raw[key].strip() or len(raw[key]) > 500:
                raise ValueError("invalid benchmark limitation disclosure")
            result[key] = raw[key]
    if "campaign_cost" in raw:
        if not isinstance(raw["campaign_cost"], dict):
            raise ValueError("invalid campaign cost")
        result["campaign_cost"] = _cost(raw["campaign_cost"])
    if "execution_valid" in raw:
        if type(raw["execution_valid"]) is not bool:
            raise ValueError("execution validity must be explicit")
        result["execution_valid"] = raw["execution_valid"]
        result["comparable"] = raw["execution_valid"] and complete
        if not raw["execution_valid"]:
            result["invalid_reason"] = _identifier(raw.get("invalid_reason"))
            for key in ("reason_zh", "reason_en"):
                value = raw.get(key)
                if value is not None:
                    if not isinstance(value, str) or not value.strip() or len(value) > 500:
                        raise ValueError("invalid execution audit explanation")
                    result[key] = value
    return result


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


class PublicTerminalBenchSource:
    def __init__(self, root: Path | None, *, ttl_s: float = 5) -> None:
        self.root = Path(root) if root else None
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._at = float("-inf")
        self._cached: dict[str, Any] | None = None
        self._good: dict[str, dict[str, Any]] = {}

    def _names(self) -> list[str]:
        assert self.root is not None
        handle = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            names = []
            with os.scandir(handle) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_ENTRIES:
                        raise ValueError("too many artifact entries")
                    if DIRECTORY.fullmatch(entry.name) and not entry.is_symlink() and entry.is_dir(follow_symlinks=False):
                        names.append(entry.name)
                        if len(names) > MAX_DIRECTORIES:
                            raise ValueError("too many Terminal-Bench directories")
            return sorted(names)
        finally:
            os.close(handle)

    def _read(self, name: str) -> bytes:
        assert self.root is not None
        handle = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handle)
            os.close(handle)
            handle = child
            fd = os.open("public-summary.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=handle)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                    raise ValueError("invalid Terminal-Bench summary file")
                content = stream.read(MAX_BYTES + 1)
                if len(content) > MAX_BYTES:
                    raise ValueError("oversized Terminal-Bench summary")
                return content
        finally:
            os.close(handle)

    def get(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if self._cached is not None and now - self._at < self.ttl_s:
                return copy.deepcopy(self._cached)
            errors = 0
            missing = 0
            good = dict(self._good)
            if self.root is not None:
                try:
                    names = self._names()
                except (OSError, ValueError):
                    names = []
                    errors += 1
                else:
                    for name in set(good) - set(names):
                        errors += 1  # Keep last valid publication if its directory disappeared.
                for name in names:
                    try:
                        content = self._read(name)
                        raw = json.loads(content, object_pairs_hook=_strict_object,
                                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
                        row = project_terminalbench_summary(raw, hashlib.sha256(content).hexdigest())
                        if any(other != name and item["run_id"] == row["run_id"] for other, item in good.items()):
                            raise ValueError("duplicate published run identity")
                        good[name] = row
                    except FileNotFoundError:
                        missing += 1
                        errors += int(name in good)
                    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                        errors += 1
            runs = sorted(good.values(), key=lambda row: (row["completed_at"], row["run_id"]), reverse=True)
            doc = {"source": "terminalbench_artifacts", "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                   "stale": bool(errors), "status": "stale" if errors else "ready" if self.root else "unavailable", "runs": runs,
                   "diagnostics": {"invalid_or_unavailable": errors, "unpublished_directories": missing}}
            self._good = good
            self._cached = doc
            self._at = now
            return copy.deepcopy(doc)
