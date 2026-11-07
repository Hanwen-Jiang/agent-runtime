#!/usr/bin/env python3
"""Prepare/merge only never-started LongMemEval cases; never call services.

The executor remains the frozen original runner. This helper preserves every
original artifact and permits no paid/partial/timeout attempt to be resubmitted.
An explicit immutable pending-reservation baseline remains unknown and charged
against the original allowance; no ledger value is changed or treated as zero.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import ExitStack, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from typing import Any, Mapping

VERSION = "longmemeval-never-started-recovery-v1"
TENANT = "evalmem20261106"


class RecoveryError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_new(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def journal(path: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[bytes]]]:
    histories, raw = defaultdict(list), defaultdict(list)
    content = path.read_bytes()
    if content and not content.endswith(b"\n"):
        raise RecoveryError("original/recovery journal has an incomplete tail; do not silently truncate")
    for line in content.splitlines(keepends=True):
        if not line.strip():
            continue
        row = json.loads(line)
        qid = row.get("question_id")
        if not isinstance(qid, str) or not qid:
            raise RecoveryError("journal question identity is missing")
        histories[qid].append(row)
        raw[qid].append(line)
    return dict(histories), dict(raw)


def never_started(history: list[dict[str, Any]]) -> bool:
    last = history[-1]
    if (last.get("outcome") != "environment_error" or last.get("error_type") != "PreflightError"
            or last.get("cleanup_complete") is not True or "lifecycle" in last
            or not isinstance(last.get("elapsed_s"), (int, float)) or not math.isfinite(last["elapsed_s"])):
        return False
    for row in history:
        # The actual runner writes one interrupted intent before factory() and
        # the caught preflight failure after it. Any lifecycle means the
        # retriever existed and may have accepted writes, even when counters=0.
        if ("lifecycle" in row or row.get("outcome") not in ("interrupted", "environment_error")
                or row.get("error_type") not in (None, "PreflightError")
                or row.get("ranked_sessions") or row.get("ranked_rounds") or row.get("turn") is not None
                or any(row.get(key) for key in ("task_id", "submitted_ids", "accepted", "attempted", "cost_usd", "actual_cost_usd"))):
            return False
    return True


def classify(histories: Mapping[str, list[dict[str, Any]]], manifest: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    selected = manifest.get("question_ids")
    if not isinstance(selected, list) or len(selected) != len(set(selected)) or set(histories) != set(selected):
        raise RecoveryError("journal must cover exactly the original frozen cohort")
    keep, pending = [], []
    for qid in selected:
        history, last = histories[qid], histories[qid][-1]
        if last.get("outcome") == "completed" and last.get("cleanup_complete") is True:
            if last.get("elapsed_s") is None or (last.get("lifecycle") or {}).get("timed_out"):
                raise RecoveryError("retained case lacks complete successful evidence")
            keep.append(qid)
        elif never_started(history):
            pending.append(qid)
        else:
            raise RecoveryError("paid, partial, timed-out or ambiguous attempts cannot be retried")
    if not pending:
        raise RecoveryError("no provably unstarted cases remain")
    return keep, pending


def usage_rows(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if snapshot.get("tenant") != TENANT or snapshot.get("missing_ledger_columns"):
        raise RecoveryError("ledger must be a complete snapshot of the isolated tenant")
    out = {}
    for value in snapshot.get("model_usage", []):
        row = dict(value)
        rid = row.get("reservation_id")
        if not isinstance(rid, str) or not rid or rid in out:
            raise RecoveryError("ledger reservation identities are invalid or duplicated")
        actual, reserved = row.get("actual_usd_micros"), row.get("reserved_usd_micros")
        if (actual is not None and (type(actual) is not int or actual < 0)
                or type(reserved) is not int or reserved < 0
                or row.get("method") != "embed" or row.get("task_id")
                or row.get("model_profile_id") != "a6api/text-embedding-3-large@2026-10"):
            raise RecoveryError("invalid or unrelated accounting in the isolated tenant")
        out[rid] = row
    return out


def pending_baseline(snapshot: Mapping[str, Any], expected_pending: Mapping[str, int],
                     *, cap_micros: int, prior_micros: int) -> dict[str, Any]:
    rows = usage_rows(snapshot)
    pending = {rid: row for rid, row in rows.items() if row["actual_usd_micros"] is None}
    if {rid: row["reserved_usd_micros"] for rid, row in pending.items()} != dict(expected_pending):
        raise RecoveryError("pending baseline differs from the explicitly authorized identities/amounts")
    if type(cap_micros) is not int or type(prior_micros) is not int or cap_micros <= 0 or prior_micros < 0:
        raise RecoveryError("invalid unchanged budget")
    if cap_micros + prior_micros != 20_000_000:
        raise RecoveryError("recovery cannot enlarge the original $20 allowance")
    return {"kind": VERSION, "tenant": TENANT, "ledger_sha256": digest(snapshot),
            "pending": pending, "pending_sha256": digest(pending),
            "cap_usd_micros": cap_micros, "historical_usd_micros": prior_micros}


def verify_ledger(snapshot: Mapping[str, Any], baseline: Mapping[str, Any],
                  baseline_ledger: Mapping[str, Any], *, require_clean: bool = True) -> dict[str, Any]:
    if (baseline.get("kind") != VERSION or baseline.get("tenant") != TENANT
            or baseline.get("ledger_sha256") != digest(baseline_ledger)
            or baseline.get("pending_sha256") != digest(baseline.get("pending"))):
        raise RecoveryError("pending baseline identity or digest changed")
    cap, prior = baseline.get("cap_usd_micros"), baseline.get("historical_usd_micros")
    if type(cap) is not int or type(prior) is not int or cap <= 0 or prior < 0 or cap + prior != 20_000_000:
        raise RecoveryError("pending baseline cannot enlarge the original allowance")
    original, rows = usage_rows(baseline_ledger), usage_rows(snapshot)
    if any(rows.get(key) != value for key, value in original.items()):
        raise RecoveryError("original ledger rows changed; reconcile instead of resetting evidence")
    pending = {rid: row for rid, row in rows.items() if row["actual_usd_micros"] is None}
    if pending != baseline["pending"]:
        raise RecoveryError("new or changed pending attempt; stop before another question")
    if require_clean and (snapshot.get("remaining_ids") or any(snapshot.get(key) != 0 for key in
            ("queue_count", "memory_item_count", "lineage_count", "review_count", "tombstone_count"))):
        raise RecoveryError("evaluation state is not completely cleaned")
    known = sum(row["actual_usd_micros"] or 0 for row in rows.values())
    retained = sum(row["reserved_usd_micros"] for row in pending.values())
    if known + retained > baseline["cap_usd_micros"]:
        raise RecoveryError("known fees plus retained unknown reservations exceed the original cap")
    return {"cost_known": not pending, "known_tenant_usd_micros": known,
            "pending_reserved_usd_micros": retained, "pending_reservation_ids": sorted(pending),
            "accounted_against_tenant_cap_usd_micros": known + retained,
            "remaining_authorized_usd_micros": baseline["cap_usd_micros"] - known - retained,
            "actual_cost_usd": known / 1e6 if not pending else None,
            "note": "Pending reserved amounts remain unknown, not settled cost or a proven actual-cost upper bound."}


@contextmanager
def inactive_original(root: Path):
    import fcntl
    with ExitStack() as stack:
        for relative in (".launcher.lock", "live/.run.lock"):
            lock = stack.enter_context((root / relative).open("rb"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RecoveryError("original experiment still has an active writer") from None
        yield


def prepare(original: Path, out: Path, *, observed_deployment: Mapping[str, Any], source_root: Path,
            data: Path, ledger: Mapping[str, Any], baseline_ledger: Mapping[str, Any],
            baseline: Mapping[str, Any]) -> dict[str, Any]:
    original, out, source_root = original.resolve(), out.resolve(), source_root.resolve()
    if original == out or original in out.parents or out.exists():
        raise RecoveryError("use a new directory outside the immutable original experiment")
    with inactive_original(original):
        freeze = json.loads((original / "freeze.json").read_text())
        manifest = json.loads((original / "live/manifest.json").read_text())
        if freeze.get("deployment") != dict(observed_deployment):
            raise RecoveryError("deployment/model/Registry/Memory identity changed")
        if (manifest.get("provenance", {}).get("freeze_sha256") != digest(freeze)
                or manifest.get("provenance", {}).get("runner_sha256") != freeze.get("runner_sha256")
                or manifest.get("provenance", {}).get("data_sha256") != freeze.get("data_sha256")
                or manifest.get("timeout_s") != freeze.get("question_timeout_s")
                or manifest.get("provenance", {}).get("seed") != freeze.get("seed")
                or manifest.get("provenance", {}).get("limit") != freeze.get("limit")):
            raise RecoveryError("original run identity is internally inconsistent")
        if file_hash(data) != freeze["data_sha256"]:
            raise RecoveryError("dataset changed")
        for name, expected in freeze["runner_sha256"].items():
            path = (source_root / name).resolve()
            if source_root not in path.parents or file_hash(path) != expected:
                raise RecoveryError("frozen source changed; do not pool versions")
        if (baseline["cap_usd_micros"] != round(freeze["remaining_allowance_usd"] * 1e6)
                or baseline["historical_usd_micros"] != round(freeze["historical_usd"] * 1e6)):
            raise RecoveryError("recovery budget differs from the original freeze")
        accounting = verify_ledger(ledger, baseline, baseline_ledger)
        histories, raw = journal(original / "live/results.jsonl")
        keep, pending = classify(histories, manifest)
        inputs = {name: file_hash(original / name) for name in ("freeze.json", "live/manifest.json", "live/results.jsonl", "live/summary.json")}
        # A single atomic mkdir claims the destination. No service or database
        # write is part of preparation; original files are only read/copied.
        out.mkdir(parents=True)
        archive, live = out / "original-evidence", out / "live"
        archive.mkdir()
        live.mkdir()
        for name in inputs:
            destination = archive / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original / name, destination)
        shutil.copyfile(original / "freeze.json", out / "freeze.json")
        shutil.copyfile(original / "live/manifest.json", live / "manifest.json")
        with (live / "results.jsonl").open("xb") as stream:
            for qid in manifest["question_ids"]:
                if qid in keep:
                    stream.writelines(raw[qid])
            stream.flush()
            os.fsync(stream.fileno())
        write_new(out / "pending-baseline.json", baseline)
        write_new(out / "ledger-baseline.json", baseline_ledger)
        receipt = {"kind": VERSION, "original": str(original), "original_file_sha256": inputs,
                   "retained_ids": keep, "unstarted_ids": pending, "question_ids": manifest["question_ids"],
                   "manifest_sha256": file_hash(live / "manifest.json"), "freeze_sha256": file_hash(out / "freeze.json"),
                   "pending_baseline_sha256": digest(baseline), "source_root": str(source_root),
                   "accounting_before": accounting,
                   "rule": "Only factory PreflightError with no lifecycle in any journal event; no paid attempt repeated."}
        write_new(out / "recovery.json", receipt)
        return receipt


def _finalize(out: Path, *, ledger: Mapping[str, Any]) -> dict[str, Any]:
    from agentruntime_learning.benchmarks import longmemeval as lme
    out = out.resolve()
    receipt = json.loads((out / "recovery.json").read_text())
    if receipt["kind"] != VERSION:
        raise RecoveryError("unknown recovery contract")
    if (file_hash(out / "freeze.json") != receipt["freeze_sha256"]
            or file_hash(out / "live/manifest.json") != receipt["manifest_sha256"]):
        raise RecoveryError("recovery changed the original freeze or manifest")
    for name, expected in receipt["original_file_sha256"].items():
        if file_hash(out / "original-evidence" / name) != expected:
            raise RecoveryError("archived original evidence changed")
    manifest = json.loads((out / "live/manifest.json").read_text())
    histories, raw = journal(out / "live/results.jsonl")
    _, old_raw = journal(out / "original-evidence/live/results.jsonl")
    if set(histories) != set(receipt["question_ids"]):
        raise RecoveryError("final results must cover exactly the full frozen cohort")
    for qid in receipt["retained_ids"]:
        if raw[qid] != old_raw[qid]:
            raise RecoveryError("a previously completed case was altered or rerun")
    rows = [histories[qid][-1] for qid in receipt["question_ids"]]
    if any(row.get("elapsed_s") is None or row.get("outcome") == "interrupted" for row in rows):
        raise RecoveryError("not every remaining case reached a terminal record")
    baseline = json.loads((out / "pending-baseline.json").read_text())
    if digest(baseline) != receipt["pending_baseline_sha256"]:
        raise RecoveryError("pending baseline was edited")
    accounting = verify_ledger(ledger, baseline, json.loads((out / "ledger-baseline.json").read_text()))
    successful = [r for r in rows if r.get("outcome") == "completed" and r.get("cleanup_complete") is True]
    # An environment failure has no measured retrieval quality. Do not include
    # its empty placeholder rankings in a model-quality denominator.
    summary = lme._summary(successful, retriever=manifest["retriever"], top_k=manifest["top_k"], ks=manifest["ks"], skipped=0)
    timeouts = sum(r.get("outcome") == "timeout" or bool(r.get("lifecycle", {}).get("timed_out")) for r in rows)
    retrieval_complete = len(successful) == len(rows) and timeouts == 0
    summary.update(selected=len(rows), completed=len(successful), timeouts=timeouts,
                   scored_questions=len(successful), cohort_questions=len(rows),
                   cleanup_complete=all(r.get("cleanup_complete") is True for r in rows),
                   outcomes={state: sum(r.get("outcome") == state for r in rows) for state in sorted({r.get("outcome") for r in rows})},
                   retrieval_complete=retrieval_complete, cost_known=accounting["cost_known"],
                   valid=retrieval_complete and accounting["cost_known"],
                   actual_cost_usd=None, accounting=accounting,
                   excluded_from_quality_as_model_failures=[r["question_id"] for r in rows if r.get("outcome") != "completed"],
                   valid_for_quality_gate=retrieval_complete,
                   acceptance_passed=False,
                   recovery={"retained_completed": len(receipt["retained_ids"]), "previously_unstarted": len(receipt["unstarted_ids"]),
                             "originals_unchanged": True, "cost_uncertainty_not_waived": True})
    # A failure is not a model zero. Keep complete-cohort metrics as descriptive
    # only; never use filtering to claim the original all-pairs gate passed.
    freeze = json.loads((out / "freeze.json").read_text())
    previous = int((freeze.get("prior_experiment") or {}).get("actual_usd_micros", 0))
    summary["known_experiment_cost_usd"] = (accounting["known_tenant_usd_micros"] - previous) / 1e6
    if accounting["cost_known"]:
        summary["actual_cost_usd"] = summary["known_experiment_cost_usd"]
    write_new(out / "recovery-summary.json", summary)
    write_new(out / "recovery-ledger-final.json", ledger)
    return summary


def finalize(out: Path, *, ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Validate only after the frozen runner releases its output writer lock."""
    import fcntl
    with (out / "live/.run.lock").open("rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RecoveryError("recovery runner is still active") from None
        return _finalize(out, ledger=ledger)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    baseline = commands.add_parser("pending-baseline")
    baseline.add_argument("--ledger", type=Path, required=True)
    baseline.add_argument("--pending-id", required=True)
    baseline.add_argument("--pending-reserved-micros", type=int, required=True)
    baseline.add_argument("--out", type=Path, required=True)
    prepare_parser = commands.add_parser("prepare")
    for name in ("original", "out", "observed-deployment", "source-root", "data", "ledger", "baseline-ledger", "pending-baseline"):
        prepare_parser.add_argument("--" + name, type=Path, required=True)
    final = commands.add_parser("finalize")
    final.add_argument("--out", type=Path, required=True)
    final.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args(argv)
    read = lambda path: json.loads(path.read_text())
    try:
        if args.command == "pending-baseline":
            result = pending_baseline(read(args.ledger), {args.pending_id: args.pending_reserved_micros},
                                      cap_micros=19_984_709, prior_micros=15_291)
            write_new(args.out, result)
            print(json.dumps({"pending_ids": sorted(result["pending"]), "ledger_unmodified": True}))
        elif args.command == "prepare":
            result = prepare(args.original, args.out, observed_deployment=read(args.observed_deployment),
                             source_root=args.source_root, data=args.data, ledger=read(args.ledger),
                             baseline_ledger=read(args.baseline_ledger), baseline=read(args.pending_baseline))
            print(json.dumps({"retained": len(result["retained_ids"]), "unstarted": len(result["unstarted_ids"]),
                              "no_model_calls": True, "out": str(args.out)}))
        else:
            result = finalize(args.out, ledger=read(args.ledger))
            print(json.dumps({"completed": result["completed"], "retrieval_complete": result["retrieval_complete"],
                              "cost_known": result["cost_known"], "acceptance_passed": False}))
    except (RecoveryError, ValueError, OSError, KeyError) as exc:
        print(type(exc).__name__ + ": " + str(exc), file=__import__("sys").stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
