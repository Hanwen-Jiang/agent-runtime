"""Read-only model_usage evidence for a frozen DeepEval subject run.

Run after collection and before paid scoring:
  python -m agentruntime_learning.evals.deepeval_ledger audit --out DIR --tenant evals

The collector uses the existing tenant-bound Postgres adapter with RLS. It never
updates reservations or replaces task records. Historical diagnostics may write
to a separate --evidence-dir so the original experiment remains untouched.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentruntime_core.tenancy import TenantContext, tenant_scope
from agentruntime_core.errors import ProviderError
from agentruntime_learning.benchmarks import swebench as sb
from agentruntime_learning.evals import deepeval_acceptance as acceptance

SCHEMA = "deepeval-model-usage-v1"
SOURCE = "model_usage via tenant-bound read-only Postgres transaction"
SCOPE = "All Gateway attempts attributed to selected Task IDs; taskless Memory, external Jev and infrastructure charges are not included"
FIELDS = ("reservation_id", "task_id", "node_id", "model_profile_id", "actual_usd_micros",
          "registry_scope", "registry_revision", "registry_document_sha256")


def _inputs(out: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    run = json.loads((out / "run.json").read_text("utf-8"))
    ids = run.get("instance_ids")
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("run needs a complete unique case selection")
    return run, sb.read_records(out)


def binding(run: Mapping[str, Any], records: Mapping[str, Any]) -> dict[str, str]:
    return {"run_sha256": acceptance.digest(run), "records_sha256": acceptance.digest(records)}


def assess(run: Mapping[str, Any], records: Mapping[str, Any], tasks: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute from saved attempts, never trust a saved 'valid' flag."""
    problems: list[str] = []
    ids = list(run["instance_ids"])
    expected: dict[str, str] = {}
    missing = []
    for cid in ids:
        record = records.get(cid) or {}
        task = record.get("task_id")
        if not isinstance(task, str) or not task:
            missing.append(cid)
        else:
            expected[cid] = task
        if record.get("state") != "finished" or not record.get("task_finished"):
            problems.append(f"task_not_terminal:{cid}")
    if missing:
        problems.append("selected_tasks_missing")
    if len(set(expected.values())) != len(expected):
        problems.append("task_reused_by_cases")
    if set(records) - set(ids):
        problems.append("records_outside_selection")
    if set(tasks) != set(expected.values()):
        problems.append("ledger_task_set_mismatch")
    try:
        frozen = acceptance.provenance((run.get("identity") or {})["deployment"])
    except (ValueError, KeyError, TypeError):
        frozen = {}
        problems.append("frozen_deployment_missing_or_invalid")
    profiles = {}
    for entry in frozen.get("registry_revisions", []):
        if entry["kind"] == "model_profile":
            if entry["name"] in profiles:
                problems.append("duplicate_registry_identity")
            profiles[entry["name"]] = entry
    models = set(frozen.get("subject_models", []))
    if models - profiles.keys():
        problems.append("frozen_model_revision_missing")

    seen: dict[str, Any] = {}
    total_micros = 0
    pending, unknown_tasks, mismatches = [], [], []
    totals: dict[str, int] = {}
    for task in sorted(set(expected.values())):
        evidence = tasks.get(task) or {}
        rows = evidence.get("rows")
        if evidence.get("error_type") or not isinstance(rows, list) or not rows:
            unknown_tasks.append(task)
            problems.append(f"ledger_missing_or_unreadable:{task}")
            continue
        totals[task] = 0
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(FIELDS):
                problems.append("invalid_ledger_row")
                unknown_tasks.append(task)
                continue
            rid = row["reservation_id"]
            if not isinstance(rid, str) or not rid or row["task_id"] != task:
                problems.append("ledger_attempt_wrong_task")
                unknown_tasks.append(task)
                continue
            if rid in seen:
                if seen[rid] != row:
                    problems.append("conflicting_reservation")
                    unknown_tasks.append(task)
                continue
            seen[rid] = row
            actual = row["actual_usd_micros"]
            if actual is None:
                pending.append(rid)
            elif isinstance(actual, bool) or not isinstance(actual, int) or actual < 0:
                problems.append("invalid_ledger_cost")
                unknown_tasks.append(task)
            else:
                total_micros += actual
                totals[task] += actual
            profile = row["model_profile_id"]
            identity = profiles.get(profile, {})
            if (
                profile not in models or row["registry_scope"] != "platform" or identity.get("scope") != "platform"
                or row["registry_revision"] != identity.get("version")
                or row["registry_document_sha256"] != identity.get("document_sha256")
            ):
                mismatches.append(rid)
    if pending:
        problems.append("ledger_pending_reservations")
    if mismatches:
        problems.append("model_or_registry_mismatch")
    cost_known = (bool(expected) and not missing and not unknown_tasks and not pending
                  and len(set(expected.values())) == len(expected) and set(tasks) == set(expected.values())
                  and not any(p.startswith("task_not_terminal:") for p in problems))
    comparisons = {}
    for cid, task in expected.items():
        reported = records[cid].get("cost_usd")
        reported_micros = None
        if isinstance(reported, (float, int)) and not isinstance(reported, bool) and math.isfinite(reported) and reported >= 0:
            reported_micros = round(reported * 1e6)
        comparisons[cid] = {
            "task_id": task, "reported_usd": reported if reported_micros is not None else None,
            "ledger_known_usd": totals.get(task, 0) / 1e6,
            "ledger_minus_reported_usd": (totals[task] - reported_micros) / 1e6 if task in totals and reported_micros is not None else None,
        }
    return {
        "valid": cost_known and not problems,
        "cost_known": cost_known,
        "known_subject_usd": total_micros / 1e6,
        "authoritative_subject_usd": total_micros / 1e6 if cost_known else None,
        "expected_tasks": expected, "attempts": len(seen), "pending_reservation_ids": sorted(set(pending)),
        "unknown_task_ids": sorted(set(unknown_tasks)), "mismatch_reservation_ids": sorted(set(mismatches)),
        "problems": sorted(set(problems)), "reported_cost_comparison": comparisons, "cost_scope": SCOPE,
    }


async def audit(out: Path, tenant: str, *, evidence_dir: Path | None = None,
                collector: Callable[[str, str], Awaitable[list[dict[str, Any]]]] | None = None) -> dict[str, Any]:
    tenant_scope(TenantContext(tenant_id=tenant))  # fail before querying with an invalid identity
    if collector is None:
        from agentruntime_learning.benchmarks.paired import collect_ledger
        collector = collect_ledger
    out = Path(out)
    run, records = _inputs(out)
    inputs = binding(run, records)
    tasks = {}
    for task in sorted({str((records.get(cid) or {})["task_id"]) for cid in run["instance_ids"] if (records.get(cid) or {}).get("task_id")}):
        try:
            rows = await collector(task, tenant)
            # Allow-list columns: never persist arbitrary database fields/credentials.
            selected = [{key: row.get(key) for key in FIELDS} for row in rows]
            tasks[task] = {"rows": sorted(selected, key=lambda row: (str(row.get("reservation_id")), acceptance.digest(row)))}
        except Exception as exc:
            tasks[task] = {"rows": [], "error_type": type(exc).__name__}
    if binding(*_inputs(out)) != inputs:
        raise ValueError("run or records changed while collecting ledger evidence; collect again")
    document = {
        "schema": SCHEMA, "source": SOURCE, "tenant_id": tenant, "binding": inputs, "tasks": tasks,
        "observed_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    document["evidence_sha256"] = acceptance.digest({k: document[k] for k in ("schema", "source", "tenant_id", "binding", "tasks")})
    document["assessment"] = assess(run, records, tasks)
    directory = Path(evidence_dir) if evidence_dir is not None else out / "ledger-evidence"
    directory.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    immutable = directory / f"audit-{document['evidence_sha256']}.json"
    if not immutable.exists():
        with immutable.open("x", encoding="utf-8") as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
    else:
        # Preserve the first observation timestamp for identical rows.
        document = json.loads(immutable.read_text("utf-8"))
        text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    sb._write_text(directory / "latest.json", text)
    return document


def status(out: Path, run: Mapping[str, Any], records: Mapping[str, Any]) -> dict[str, Any]:
    path = Path(out) / "ledger-evidence/latest.json"
    missing = {"valid": False, "cost_known": False, "authoritative_subject_usd": None, "known_subject_usd": None,
               "problems": ["ledger_evidence_missing"], "source": SOURCE, "cost_scope": SCOPE}
    if not path.exists():
        return missing
    try:
        document = json.loads(path.read_text("utf-8"))
        tenant_scope(TenantContext(tenant_id=document["tenant_id"]))
        content = {k: document[k] for k in ("schema", "source", "tenant_id", "binding", "tasks")}
        if (
            document["schema"] != SCHEMA or document["source"] != SOURCE
            or document["binding"] != binding(run, records)
            or not isinstance(document["tasks"], dict)
            or document.get("evidence_sha256") != acceptance.digest(content)
        ):
            raise ValueError("stale or mismatched evidence")
        assessed = assess(run, records, document["tasks"])
        return {**assessed, "source": SOURCE, "tenant_id": document["tenant_id"],
                "evidence_sha256": document["evidence_sha256"], "observed_at": document["observed_at"]}
    except (OSError, ValueError, KeyError, TypeError, ProviderError):
        return {**missing, "problems": ["ledger_evidence_invalid_or_stale"]}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit",))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--evidence-dir", type=Path, help="Use a new location for read-only historical diagnostics")
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(audit(args.out, args.tenant, evidence_dir=args.evidence_dir))
        print(json.dumps({"evidence_sha256": result["evidence_sha256"], **result["assessment"]}, ensure_ascii=False, indent=2))
        return 0 if result["assessment"]["valid"] else 4
    except Exception as exc:
        # Provider exception text may contain DSNs or endpoint credentials.
        print(f"DeepEval ledger audit failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
