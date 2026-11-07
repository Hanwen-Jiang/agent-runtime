"""N16-R / EV-REAL paired Task API experiments; no production config mutations.

Run each arm against a separately frozen stack (or an operator-switched stack),
using the same plan, seed and cases. snapshot_command must inspect live state and
emit provenance JSON. This runner rejects drift before and after every task.
All costs come from model_usage, including failed provider attempts; SSE evidence
counts visible tool calls and Jev vetoes, never infers unobserved actions.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import random
import re
import statistics
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agentruntime_learning.benchmarks import provenance as pv
from agentruntime_learning.benchmarks import swebench as sb
from agentruntime_learning.benchmarks.client import Event, RuntimeClient, token_from_env

RUBRIC_VERSION = "paired-factual-checklist-v1"
JEV_CODES = {"rule_judge_disowns", "rule_judge_off_topic"}


@dataclasses.dataclass(frozen=True)
class Case:
    instance_id: str
    message: str
    required_patterns: tuple[str, ...]
    should_delegate: bool
    files: tuple[dict[str, str], ...] = ()
    repo: str = "paired-evaluation"


def load_cases(rows: Sequence[Mapping[str, Any]]) -> list[Case]:
    seen: set[str] = set()
    out = []
    for row in rows:
        cid = str(row["id"])
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,50}", cid) or cid in seen:
            raise ValueError("case id must be unique and path-safe")
        seen.add(cid)
        patterns = tuple(row["required_patterns"])
        if not patterns or not isinstance(row.get("should_delegate"), bool):
            raise ValueError("freeze factual checks and delegation labels before running")
        for pattern in patterns:
            re.compile(pattern)
        files = tuple(dict(f) for f in row.get("files", []))
        for item in files:
            p = Path(item["path"])
            if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] == ".git":
                raise ValueError("fixture path must be a relative workspace file")
        out.append(Case(cid, str(row["message"]), patterns, row["should_delegate"], files))
    if not out:
        raise ValueError("empty case set")
    return out


def snapshot(argv: Sequence[str]) -> dict[str, Any]:
    if not argv or isinstance(argv, str):
        raise ValueError("snapshot_command must be a nonempty argv array")
    # No shell interpolation; stderr may contain deployment credentials.
    proc = subprocess.run(list(argv), capture_output=True, timeout=60, check=False)
    if proc.returncode:
        raise RuntimeError(f"runtime snapshot failed (exit {proc.returncode}); inspect it separately")
    return pv.validate(json.loads(proc.stdout))


from agentruntime_learning.evals.deliverables import _Runner as EvidenceRunner


class Runner(EvidenceRunner):
    def _workspace_args(self, inst: Any) -> tuple[str, str, list[dict[str, str]]]:
        return "", "", list(inst.files)

    def _message(self, inst: Any) -> str:
        return str(inst.message)

    def _follow(self, rec: dict[str, Any], *, interactive: bool) -> dict[str, Any]:
        rec["observations"] = []  # base follows from event zero; rebuild on reconnect/resume
        return super()._follow(rec, interactive=interactive)

    def _observe_event(self, rec: dict[str, Any], ev: Event) -> None:
        super()._observe_event(rec, ev)
        if ev.name not in ("node_started", "node_finished", "tool_status", "verdict", "stage_started"):
            return
        item = {"event_id": ev.id, "event": ev.name}
        for key in ("node_id", "round_seq", "mode", "kind", "role", "state", "step", "tool", "reason_code", "verdict"):
            if key in ev.value:
                item[key] = ev.value[key]
        # Targets can contain command fragments; store only a digest for repetition.
        if ev.name == "tool_status" and ev.value.get("target"):
            item["target_sha256"] = pv.digest(str(ev.value["target"]))
        rec["observations"].append(item)


def observations(rec: Mapping[str, Any]) -> dict[str, Any]:
    events = {int(e["event_id"]): e for e in rec.get("observations", [])}.values()
    sub_nodes = sorted({e["node_id"] for e in events if e["event"] == "node_started" and e.get("mode") == "sub_agent"})
    tools = [e for e in events if e["event"] == "tool_status" and e.get("state") == "running"]
    counts = Counter((e["tool"], e["target_sha256"]) for e in tools if e.get("target_sha256"))
    vetoes = [e for e in events if e.get("reason_code") in JEV_CODES]
    return {"sub_agent_nodes": sub_nodes, "tool_calls_observed": len(tools),
            "unsupported_subtasks": sorted({e["node_id"] for e in events if e["event"] == "node_started" and e.get("mode") == "sub_task"}),
            "repeated_tool_targets": sum(n - 1 for n in counts.values()),
            "targetless_tool_calls": sum(not e.get("target_sha256") for e in tools),
            "jev_vetoes": len(vetoes), "jev_veto_rounds": sorted({e.get("round_seq") for e in vetoes}),
            "extra_rounds": max(0, int(rec.get("rounds") or 0) - 1),
            "evidence_scope": "public SSE; repeated targets are a proxy, not identical full arguments"}


def resolved_attempt_role(row: Mapping[str, Any], evidence: Mapping[str, Any]) -> str | None:
    """Resolve a model attempt against actual activity inputs and its time window."""
    if not evidence.get("complete"):
        return None
    at = row.get("reserved_at")
    if isinstance(at, bool) or not isinstance(at, int):
        return None
    node = str(row.get("node_id") or "")
    special = "summary" if "#summary" in node else "probe" if "#probe" in node else None
    base = node.split("#" + special, 1)[0] if special else node
    candidates = [
        observation for observation in evidence.get("observations", [])
        if observation.get("task_id") == row.get("task_id")
        and observation.get("context_node_id" if special else "node_id") == base
        and isinstance(observation.get("scheduled_at_unix_ms"), int)
        and isinstance(observation.get("terminal_at_unix_ms"), int)
        and observation["scheduled_at_unix_ms"] <= at <= observation["terminal_at_unix_ms"]
    ]
    roles = {observation["role"] for observation in candidates}
    return (special or next(iter(roles))) if len(roles) == 1 else None


def ledger_metrics(rows: Sequence[Mapping[str, Any]], sub_nodes: Sequence[str], frozen: Mapping[str, Any],
                   node_roles: Mapping[str, str] | None = None,
                   role_evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    # reservation_id makes retries of evidence collection idempotent.
    unique: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        rid = str(row["reservation_id"])
        if rid in unique and unique[rid] != row:
            raise ValueError("conflicting model_usage reservation")
        unique[rid] = row
    pending = [r for r in unique.values() if r.get("actual_usd_micros") is None]
    bad = []
    model_mismatch = []
    unresolved_roles = []
    total = child = 0
    for row in unique.values():
        node = str(row.get("node_id") or "")
        is_child = any(node == n or node.startswith(n + ".") or node.startswith(n + "#") for n in sub_nodes)
        cost = row.get("actual_usd_micros")
        if cost is not None:
            if int(cost) < 0:
                raise ValueError("negative ledger charge")
            total += int(cost)
            if is_child:
                child += int(cost)
        profile = str(row.get("model_profile_id") or "")
        role = resolved_attempt_role(row, role_evidence) if role_evidence is not None else {"plan": "plan", "triage": "route", "review": "judge"}.get(node)
        if role_evidence is not None:
            if role is None:
                unresolved_roles.append(str(row["reservation_id"]))
        elif node.endswith("#summary"):
            role = "summary"
        elif node.endswith("#probe"):
            role = "probe" if "probe" in frozen["models"] else "judge"
        elif is_child:
            role = "sub_agent"
        if role is None and role_evidence is None:
            # Public node events identify ordinary top-level roles; expanded
            # tool-loop nodes inherit the longest matching parent role.
            parents = [n for n in (node_roles or {}) if node == n or node.startswith(n + ".")]
            role = (node_roles or {})[max(parents, key=len)] if parents else "act"
        if role is not None and frozen["models"].get(role) != profile:
            model_mismatch.append(str(row["reservation_id"]))
        identity = frozen["registry"].get(profile)
        if not identity or any(str(row.get(k) or "") != str(identity[v]) for k, v in (
                ("registry_scope", "scope"), ("registry_revision", "revision"), ("registry_document_sha256", "document_sha256"))):
            bad.append(str(row["reservation_id"]))
    return {"total_cost_usd": total / 1_000_000, "sub_agent_cost_usd": child / 1_000_000,
            "attempts": len(unique), "pending": len(pending), "known": bool(unique) and not pending,
            "registry_mismatch_reservations": bad, "source": "model_usage",
            "model_mismatch_reservations": model_mismatch,
            "unresolved_role_reservations": unresolved_roles,
            "cost_scope": "Model Gateway attempts; external Jev and infrastructure charges are not included",
            "models_served": sorted({str(r["model_profile_id"]) for r in unique.values()})}


async def collect_ledger(task_id: str, tenant_id: str) -> list[dict[str, Any]]:
    from agentruntime_core.adapters.postgres import PostgresRelationalStoreProvider
    from agentruntime_core.tenancy import TenantContext

    dsn = os.environ.get("AR_RELATIONAL_DSN", "")
    if not dsn:
        raise ValueError("AR_RELATIONAL_DSN is required for authoritative attempt costs")
    store = await PostgresRelationalStoreProvider.connect(dsn)
    try:
        async with store.bind(TenantContext(tenant_id=tenant_id)).transaction(read_only=True) as tx:
            rows = await tx.query(
                "SELECT reservation_id, task_id, node_id, model_profile_id, actual_usd_micros, reserved_at, "
                "registry_scope, registry_revision, registry_document_sha256 "
                "FROM model_usage WHERE task_id = $1", [task_id])
        return [dict(r) for r in rows]
    finally:
        await store.aclose()


async def collect_temporal(task_id: str, tenant_id: str | None = None) -> dict[str, Any]:
    """Read child identities and authoritative elapsed time, without payloads.

    No model prompts, tool arguments, claim-check bodies or credentials are
    exported. Child workflow ids follow stage_plan.subworkflow_id's contract.
    """
    from temporalio.client import Client

    address = os.environ.get("AR_TEMPORAL_ADDRESS", "")
    if not address:
        raise ValueError("AR_TEMPORAL_ADDRESS is required for complete child evidence")
    client = await Client.connect(address, namespace=os.environ.get("AR_TEMPORAL_NAMESPACE", "agent-runtime"))
    queue = [task_id]
    seen: set[str] = set()
    children: set[str] = set()
    complete = True
    elapsed = None
    planning = None
    model_roles = []
    workflow_runs = {}
    while queue:
        workflow_id = queue.pop()
        if workflow_id in seen:
            continue
        seen.add(workflow_id)
        if len(seen) > 128:
            raise ValueError("unexpectedly large workflow tree")
        described = await client.get_workflow_handle(workflow_id).describe()
        workflow_runs[workflow_id] = described.run_id
        history = await client.get_workflow_handle(workflow_id, run_id=described.run_id).fetch_history()
        events = history.events
        if tenant_id is not None:
            from agentruntime_learning.benchmarks.tool_history_evidence import model_role_evidence
            from agentruntime_worker.replay import claim_check_from_env
            model_roles.append(await model_role_evidence(history, tenant_id, run_id=described.run_id, claim_check=claim_check_from_env()))
        if workflow_id == task_id and tenant_id is not None:
            from agentruntime_learning.benchmarks.paired_completion import plan_evidence
            from agentruntime_worker.replay import claim_check_from_env
            planning = await plan_evidence(history, tenant_id, claim_check=claim_check_from_env())
        closed = any(e.HasField(field) for e in events for field in (
            "workflow_execution_completed_event_attributes", "workflow_execution_failed_event_attributes",
            "workflow_execution_canceled_event_attributes", "workflow_execution_timed_out_event_attributes",
            "workflow_execution_terminated_event_attributes"))
        complete = complete and closed
        if workflow_id == task_id and closed and events:
            elapsed = (events[-1].event_time.ToDatetime() - events[0].event_time.ToDatetime()).total_seconds()
        for event in events:
            if not event.HasField("start_child_workflow_execution_initiated_event_attributes"):
                continue
            child = event.start_child_workflow_execution_initiated_event_attributes
            if child.workflow_type.name in ("SubAgentWorkflow", "SubgraphWorkflow"):
                if not child.workflow_id.startswith(task_id + "/r"):
                    raise ValueError("unexpected child identity")
                queue.append(child.workflow_id)
                if child.workflow_type.name == "SubAgentWorkflow":
                    children.add(child.workflow_id.split("/", 2)[2].split("/", 1)[0])
    from agentruntime_learning.benchmarks.tool_history_evidence import merge_model_roles
    return {"sub_agent_nodes": sorted(children), "elapsed_s": elapsed, "complete": complete,
            "workflow_ids": sorted(seen), "source": "Temporal histories", "planning_evidence": planning,
            "workflow_runs": workflow_runs,
            "model_role_evidence": merge_model_roles(model_roles, task_id) if model_roles else None}


def score_case(case: Case, rec: Mapping[str, Any]) -> dict[str, Any]:
    text = str(rec.get("deliverable") or "")
    hits = [bool(re.search(pattern, text, re.I | re.M)) for pattern in case.required_patterns]
    obs = observations(rec)
    return {**obs, "factual_checklist": hits, "quality": sum(hits) / len(hits),
            "delegation_correct": bool(obs["sub_agent_nodes"]) == case.should_delegate,
            "rubric": RUBRIC_VERSION}


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sb._write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def validate_plan(plan: Mapping[str, Any]) -> list[Case]:
    if set(plan["arms"]) != {"off", "rules"}:
        raise ValueError("both off and rules arms are required")
    for arm, cfg in plan["arms"].items():
        if pv.validate(cfg["provenance"])["sub_agent_policy"] != arm or not cfg.get("snapshot_command"):
            raise ValueError("each arm needs matching provenance and a live snapshot command")
    if not pv.comparable(*(plan["arms"][a]["provenance"] for a in ("off", "rules")), paired=True):
        raise ValueError("paired arms differ in more than sub_agent_policy")
    thresholds = plan["thresholds"]
    if not 0 < float(thresholds["minimum_quality_gain"]) <= 1 or float(thresholds["max_cost_ratio"]) < 1:
        raise ValueError("quality gain and acceptable cost multiplier must be frozen before running")
    if float(plan["budget_usd"]) > 40 or float(plan["budget_usd"]) <= 0:
        raise ValueError("EV-REAL/N16-R approved shared cap is $40")
    if not 0 < float(plan["task_usd_limit"]) <= float(plan["budget_usd"]):
        raise ValueError("invalid per-task cap")
    if "completion" in plan:
        from agentruntime_learning.benchmarks.paired_completion import validate_completion
        validate_completion(plan)
    return load_cases(plan["cases"])


def estimate(plan: Mapping[str, Any]) -> dict[str, Any]:
    cases = validate_plan(plan)
    history = pv.historical_cost([Path(p) for p in plan.get("historical_runs", [])])
    from agentruntime_learning.benchmarks.paired_completion import spending
    return {"new_tasks": 2 * len(cases), "historical": history,
            **spending(plan, history), "quality_metric": RUBRIC_VERSION}



def run_arm(plan: Mapping[str, Any], out: Path, arm: str, *, runtime: Any = None, inspect: Any = snapshot, ledger: Any = None,
            histories: Any = None) -> dict[str, Any]:
    cases = validate_plan(plan)
    frozen_plan = {**dict(plan), "rubric_version": RUBRIC_VERSION}
    manifest = out / "paired-manifest.json"
    if manifest.exists() and json.loads(manifest.read_text()) != frozen_plan:
        raise sb.ResumeMismatch("paired plan/configuration/rubric changed; choose a new directory")
    _write(manifest, frozen_plan)
    cfg = plan["arms"][arm]
    expected = pv.validate(cfg["provenance"])
    if inspect(cfg["snapshot_command"]) != expected:
        raise ValueError("live runtime does not match frozen configuration")
    estimation = estimate(plan)
    historical = estimation["historical"]
    if historical["unknown_task_ids"]:
        raise ValueError("historical cost unknown; reconcile model_usage before spending")
    runtime = runtime or RuntimeClient(cfg["task_api"], token_from_env())
    ledger = ledger or (lambda task: asyncio.run(collect_ledger(task, str(plan["tenant_id"]))))
    histories = histories or (lambda task: asyncio.run(collect_temporal(task, str(plan["tenant_id"]))))
    random.Random(int(plan.get("seed", 0))).shuffle(cases)
    for case in cases:
        run_dir = out / arm / case.instance_id
        metric_path = run_dir / "metrics.json"
        if metric_path.exists() and json.loads(metric_path.read_text()).get("valid"):
            continue
        recs = sb.read_records(run_dir)
        existing = recs.get(case.instance_id, {})
        spent = historical["known_usd"]
        for path in out.glob("*/*/metrics.json"):
            if path == metric_path:
                continue
            metric = json.loads(path.read_text())
            if not metric["ledger"]["known"] or not metric.get("valid"):
                raise ValueError("previous evidence is unsettled or invalid; reconcile it before further tasks")
            spent += metric["ledger"]["total_cost_usd"]
        for record_path in out.glob("*/*/records/*.json"):
            prior = json.loads(record_path.read_text())
            prior_run = record_path.parent.parent
            if prior.get("task_id") and prior_run != run_dir and not (prior_run / "metrics.json").exists():
                raise ValueError("another task has uncollected ledger evidence; resume its arm before spending")
        # Existing tasks are reconciled even when the remaining budget is zero.
        if not existing.get("task_id") and spent + float(plan["task_usd_limit"]) > historical["known_usd"] + estimation["task_gateway_budget_usd"]:
            break
        if inspect(cfg["snapshot_command"]) != expected:
            raise ValueError("runtime drift detected before task")
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "records").mkdir(exist_ok=True)
        run_id = "pair-" + pv.digest([plan["run_id"], arm, case.instance_id])[:24]
        settings = sb.Settings(run_id=run_id, budget_usd=float(plan["task_usd_limit"]), task_usd_limit=float(plan["task_usd_limit"]),
                               environment="none", task_timeout_s=float(plan.get("task_timeout_s", 900)), provenance=expected,
                               model_name=str(expected["models"]["act"]))
        started = settings.clock()
        runner = Runner(runtime, [case], run_dir, settings)
        runner.go()
        rec = sb.read_records(run_dir)[case.instance_id]
        post = inspect(cfg["snapshot_command"])
        metrics = {"case_id": case.instance_id, "arm": arm, "task_id": rec.get("task_id"), "status": rec.get("status"),
                   "elapsed_s": settings.clock() - started, "wall_time_complete": not bool(existing.get("task_id")),
                   "runtime_stable": post == expected, "provenance_sha256": pv.digest(expected), **score_case(case, rec)}
        if not rec.get("task_id"):
            _write(run_dir / "failure.json", {"state": rec.get("state"), "error": rec.get("error")})
            raise RuntimeError("task was not submitted; inspect failure.json")
        rows = ledger(rec["task_id"])
        _write(run_dir / "model_usage.json", rows)
        temporal = histories(rec["task_id"])
        _write(run_dir / "temporal-evidence.json", temporal)
        if "completion" in plan:
            from agentruntime_learning.benchmarks.paired_completion import planning_score, verify_jev_model
            verify_jev_model((temporal.get("planning_evidence") or {}).get("judge_usage", {}).get("attempts", []),
                             plan.get("expected_jev_model"))
            metrics["planning"] = planning_score(temporal.get("planning_evidence") or {}, should_delegate=case.should_delegate, arm=arm)
        metrics["sub_agent_nodes"] = temporal["sub_agent_nodes"]
        metrics["delegation_correct"] = bool(temporal["sub_agent_nodes"]) == case.should_delegate
        metrics["elapsed_s"] = temporal["elapsed_s"]
        metrics["wall_time_complete"] = bool(temporal["complete"])
        roles = {e["node_id"]: e.get("role") or "act" for e in rec.get("observations", []) if e["event"] == "node_started"}
        metrics["ledger"] = ledger_metrics(rows, metrics["sub_agent_nodes"], expected, roles, temporal.get("model_role_evidence"))
        metrics["valid"] = (rec.get("state") == "finished" and post == expected and metrics["ledger"]["known"]
                            and temporal["complete"] and temporal["elapsed_s"] is not None
                            and not metrics["unsupported_subtasks"]
                            and not metrics["ledger"]["registry_mismatch_reservations"]
                            and not metrics["ledger"]["model_mismatch_reservations"]
                            and not metrics["ledger"]["unresolved_role_reservations"]
                            and ("completion" not in plan or (temporal.get("model_role_evidence") or {}).get("complete"))
                            and ("completion" not in plan or metrics["planning"]["complete"]))
        _write(metric_path, metrics)
        if not metrics["valid"]:
            raise RuntimeError("incomplete or mismatched evidence; no further tasks submitted")
    return report(out)


def report(out: Path) -> dict[str, Any]:
    plan = json.loads((out / "paired-manifest.json").read_text())
    cases = validate_plan(plan)
    pairs = []
    invalid = []
    for case in cases:
        arms = {}
        for arm in ("off", "rules"):
            path = out / arm / case.instance_id / "metrics.json"
            if path.exists():
                arms[arm] = json.loads(path.read_text())
        if len(arms) != 2 or not all(m.get("valid") for m in arms.values()):
            invalid.append(case.instance_id)
            continue
        pairs.append({"case_id": case.instance_id, **arms})
    means = {}
    for arm in ("off", "rules"):
        rows = [p[arm] for p in pairs]
        means[arm] = {key: statistics.mean(float(r[key]) for r in rows) if rows else None
                      for key in ("quality", "delegation_correct", "elapsed_s", "extra_rounds", "repeated_tool_targets", "jev_vetoes")}
        means[arm].update({key: sum(r["ledger"][key] for r in rows) for key in ("total_cost_usd", "sub_agent_cost_usd")})
    ratio = means["rules"]["total_cost_usd"] / means["off"]["total_cost_usd"] if means["off"]["total_cost_usd"] else None
    gain = means["rules"]["quality"] - means["off"]["quality"] if pairs else None
    passed = not invalid and ratio is not None and gain is not None and gain >= plan["thresholds"]["minimum_quality_gain"] and ratio <= plan["thresholds"]["max_cost_ratio"]
    result = {"paired_cases": len(pairs), "incomplete_or_invalid": invalid, "means": means, "quality_gain": gain, "cost_ratio": ratio,
              "n16_r_quality_and_gateway_cost_gate": passed,
              "acceptance_requires": ["separate reconciliation of external Jev costs", "semantic correctness review; factual checklist alone is insufficient"],
              "ev_real": {"delegation_accuracy": means["rules"]["delegation_correct"], "quality_metric": RUBRIC_VERSION},
              "jev_repeat_veto_regression": any(p[a]["jev_vetoes"] > 1 for p in pairs for a in ("off", "rules")),
              "pairs": pairs, "limitations": ["SSE repeated targets are not full argument equality", "factual checklist is not a semantic DeepEval score",
                  "cost totals cover Model Gateway attempts; external Jev billing must be separately reconciled before a total-cost acceptance",
                  "one Jev veto can still occur after f06e30c; no claim that the old $1.97 task is cheaper", "interrupted runs have incomplete wall time"]}
    _write(out / "report.json", result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("estimate", "run", "report"))
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--arm", choices=("off", "rules"))
    args = parser.parse_args(argv)
    if args.command == "report":
        result = report(args.out)
    else:
        plan = json.loads(args.plan.read_text())
        result = estimate(plan)
        print(json.dumps(result, ensure_ascii=False))
        if args.command == "run":
            if not args.arm or not args.out:
                parser.error("run requires --arm and --out")
            # Both arms share an exclusive local lock: no duplicate spending.
            import fcntl
            args.out.mkdir(parents=True, exist_ok=True)
            with (args.out / ".run.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = run_arm(plan, args.out, args.arm)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
