#!/usr/bin/env python3
"""Run the frozen LongMemEval comparison against an isolated live Memory tenant.

This script reads Kubernetes/Registry/ledger state; it never deploys or changes
policies. Only `run` writes evaluation memories and may spend model allowance.
Secrets stay in process memory, never command arguments or output artifacts.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TENANT = "evalmem20261106"
MODEL = "a6api/text-embedding-3-large@2026-10"
CAP_MICROS = 19_984_709
PRIOR_MICROS = 15_291
ADMIT_MICROS = CAP_MICROS - 3_000_000
DATA_SHA256 = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
DATA = Path("/opt/evals/longmemeval-livecheck-20261107/longmemeval_s_cleaned.json")
LEXICAL = DATA.parent / "lexical-fixed"
ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "agent-runtime"


class PreflightError(RuntimeError):
    pass


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2)
        fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
    os.replace(temporary, path)


def kubectl(*argv: str, stdin: str | None = None) -> bytes:
    env = dict(os.environ)
    env.setdefault("KUBECONFIG", "/etc/rancher/k3s/k3s.yaml")
    result = subprocess.run(["kubectl", "-n", NAMESPACE, *argv], input=stdin.encode() if stdin is not None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90, env=env)
    if result.returncode:
        # kubectl stderr and server exceptions may contain DSNs or credentials.
        raise PreflightError(f"kubectl {argv[0]} failed with exit {result.returncode}; inspect service separately")
    return result.stdout


def resource(kind: str, name: str) -> dict[str, Any]:
    return json.loads(kubectl("get", kind, name, "-o", "json"))


def db_snapshot(ids: list[str] | None = None) -> dict[str, Any]:
    # Run with the service's non-superuser DSN and normal row-level tenant scope.
    # Do not read/decrypt memory contents, SecretStore values, prompts or outputs.
    args = json.dumps({"tenant": TENANT, "ids": ids or [], "model": MODEL, "ids_only": ids is not None})
    program = '''import json,os,hashlib,psycopg
args=json.loads(ARGS)
with psycopg.connect(os.environ["AR_RELATIONAL_DSN"]) as c:
 c.execute("SET TRANSACTION READ ONLY")
 role=c.execute("select rolsuper,rolbypassrls from pg_roles where rolname=current_user").fetchone()
 if role[0] or role[1]: raise RuntimeError("evaluation requires an RLS-bound service role")
 c.execute("select set_config(%s,%s,true)",("agentruntime.tenant_id",args["tenant"]))
 columns=[r[0] for r in c.execute("select column_name from information_schema.columns where table_name='model_usage'").fetchall()]
 required=["reservation_id","reserved_at","model_profile_id","method","task_id","actual_usd_micros","reserved_usd_micros","registry_scope","registry_revision","registry_document_sha256"]
 missing=sorted(set(required)-set(columns))
 usage=[] if missing or args["ids_only"] else [dict(zip(required,row)) for row in c.execute("select "+",".join(required)+" from model_usage order by reserved_at").fetchall()]
 queue=c.execute("select count(*) from memory_queue").fetchone()[0]
 remaining=[r[0] for r in c.execute("select id from memory_items where id=ANY(%s)",(args["ids"],)).fetchall()]
 policy=c.execute("select version,document from registry_entries where kind=5 and state=2").fetchall()
 c.execute("select set_config(%s,%s,true)",("agentruntime.tenant_id","_platform"))
 profile=c.execute("select version,document from registry_entries where kind=4 and name=%s and state=2",(args["model"],)).fetchall()
 def entries(rows): return [{"version":"v"+str(v),"document_sha256":hashlib.sha256(d.encode()).hexdigest(),"document":json.loads(d)} for v,d in rows]
 print(json.dumps({"tenant":args["tenant"],"missing_ledger_columns":missing,"queue_count":queue,"remaining_ids":remaining,"model_usage":usage,"tenant_policy":entries(policy),"profile":entries(profile)}))
'''.replace("ARGS", repr(args))
    return json.loads(kubectl("exec", "-i", "deploy/ar-agent-runtime-memory", "--", "python", "-", stdin=program))


def deployment_snapshot(component: str) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = json.loads(kubectl("get", "deployment,pods", "-l", f"app.kubernetes.io/component={component}", "-o", "json"))
    deploys = [v for v in raw["items"] if v["kind"] == "Deployment"]
    pods = [v for v in raw["items"] if v["kind"] == "Pod"]
    if len(deploys) != 1 or len(pods) != 1:
        raise PreflightError(f"{component} is not an isolated stable single replica")
    d, p = deploys[0], pods[0]
    status, meta = d.get("status", {}), d["metadata"]
    if (d["spec"].get("replicas") != 1 or any(status.get(k) != 1 for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"))
            or status.get("observedGeneration", 0) < meta.get("generation", 1)
            or p["metadata"].get("deletionTimestamp") or p.get("status", {}).get("phase") != "Running"
            or not any(c.get("type") == "Ready" and c.get("status") == "True" for c in p.get("status", {}).get("conditions", []))):
        raise PreflightError(f"{component} deployment is changing or not ready")
    containers = d["spec"]["template"]["spec"]["containers"]
    env = {v["name"]: v.get("value") for c in containers for v in c.get("env", [])}
    image_ids = sorted(c.get("imageID", "") for c in p["status"].get("containerStatuses", []))
    if not image_ids or any("sha256:" not in value for value in image_ids):
        raise PreflightError(f"{component} actual image digest is unavailable")
    return raw, {"deployment_uid": meta["uid"], "generation": meta["generation"], "pod_uid": p["metadata"]["uid"],
                 "pod_spec_sha256": digest(d["spec"]), "images": [c["image"] for c in containers], "image_ids": image_ids,
                 "configmaps": {v["name"]: v["configMap"]["name"] for v in d["spec"]["template"]["spec"].get("volumes", []) if "configMap" in v},
                 "safe_env": {k: env[k] for k in ("AR_EMBEDDING_VERSION", "AR_MODEL_GATEWAY_ADDRESS", "AR_MILVUS_URI", "AR_MODEL_PROFILES_REQUIRE_REGISTRY", "AR_REGISTRY_ADDRESS") if k in env}}


def check_money(rows: list[dict[str, Any]], *, before_question: bool) -> dict[str, Any]:
    actual = sum(r["actual_usd_micros"] or 0 for r in rows)
    reserved = sum(max(r["actual_usd_micros"] or 0, r["reserved_usd_micros"]) for r in rows)
    pending = sum(r["actual_usd_micros"] is None for r in rows)
    if any(r["model_profile_id"] != MODEL or r["method"] != "embed" or r.get("task_id") for r in rows):
        raise PreflightError("isolated tenant contains unrelated model traffic")
    if pending:
        raise PreflightError("model cost pending; reconcile ledger before new work")
    if actual > CAP_MICROS or before_question and reserved >= ADMIT_MICROS:
        raise PreflightError("approved experiment budget admission refused")
    return {"actual_usd": actual / 1e6, "historical_usd": PRIOR_MICROS / 1e6,
            "total_including_history_usd": (actual + PRIOR_MICROS) / 1e6,
            "conservative_reserved_usd": reserved / 1e6, "pending": pending, "attempts": len(rows)}


def validate_registry(db: dict[str, Any], profile_file: dict[str, Any], policy_file: dict[str, Any]) -> dict[str, Any]:
    if db["missing_ledger_columns"] or len(db["profile"]) != 1 or len(db["tenant_policy"]) != 1:
        raise PreflightError("published profile/policy or R06 ledger evidence is missing")
    entry, policy = db["profile"][0], db["tenant_policy"][0]
    expected = {"scope": "platform", "version": entry["version"], "sha256": entry["document_sha256"]}
    if profile_file.get("registry_revisions", {}).get(MODEL) != expected:
        raise PreflightError("Gateway export differs from actual Registry publication")
    candidates = [p for p in profile_file.get("profiles", []) if p.get("id") == MODEL]
    if candidates != [entry["document"]]:
        raise PreflightError("Gateway embedding document differs from Registry")
    if entry["document"].get("cost", {}).get("in_per_m") != 0.13:
        raise PreflightError("embedding tariff changed; recalculate frozen estimate")
    target = policy_file.get("tenants", {}).get(TENANT, {})
    for document in (policy["document"], target):
        if document.get("model_allowlist") != [MODEL]:
            raise PreflightError("isolated tenant must allow only the frozen embedding model")
        budget = document.get("model_budget", {})
        if any(round(float(budget.get(k, -1)) * 1e6) != CAP_MICROS for k in ("daily_usd", "monthly_usd")):
            raise PreflightError("isolated tenant hard budget differs from remaining authorization")
    for row in db["model_usage"]:
        if (row.get("registry_scope"), row.get("registry_revision"), row.get("registry_document_sha256")) != ("platform", entry["version"], entry["document_sha256"]):
            raise PreflightError("a real attempt used a different or unknown Registry revision")
    return {"profile": {k: v for k, v in entry.items() if k != "document"}, "policy": {k: v for k, v in policy.items() if k != "document"}}


def observe(args: argparse.Namespace, *, before_question: bool = False, ids: list[str] | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    topology, memory = deployment_snapshot("memory")
    _, gateway = deployment_snapshot("model-gateway")
    for snapshot, revision, label in ((memory, args.memory_commit, "Memory"), (gateway, args.gateway_commit, "Gateway")):
        tags = [re.search(r"(?:^|[-:])([a-f0-9]{7,40})$", image) for image in snapshot["images"]]
        if not re.fullmatch(r"[a-f0-9]{40}", revision) or not all(tag and revision.startswith(tag.group(1)) for tag in tags):
            raise PreflightError(f"{label} image tag does not match provided full source revision")
    if memory["safe_env"].get("AR_EMBEDDING_VERSION") != MODEL or gateway["safe_env"].get("AR_MODEL_PROFILES_REQUIRE_REGISTRY") != "true":
        raise PreflightError("deployed Memory model or Gateway Registry enforcement differs")
    profile_cm = resource("configmap", gateway["configmaps"]["model-profiles"])
    if profile_cm.get("immutable") is not True:
        raise PreflightError("Gateway profile ConfigMap must be immutable during the experiment")
    profiles_raw = profile_cm["data"]["model-profiles.json"]
    policies_raw = resource("configmap", gateway["configmaps"]["tenant-policies"])["data"]["tenant-policies.json"]
    db = db_snapshot(ids)
    identity = validate_registry(db, json.loads(profiles_raw), json.loads(policies_raw))
    money = check_money(db["model_usage"], before_question=before_question)
    if db["queue_count"] or db["remaining_ids"]:
        raise PreflightError("isolated Memory queue or submitted memory IDs remain")
    service = resource("service", "ar-agent-runtime-memory")
    port = next(v["port"] for v in service["spec"]["ports"] if v["port"] == 50061)
    frozen = {"tenant": TENANT, "memory": memory, "gateway": gateway, "registry": identity,
              "gateway_profiles_sha256": hashlib.sha256(profiles_raw.encode()).hexdigest(),
              "gateway_policy_file_sha256": hashlib.sha256(policies_raw.encode()).hexdigest(),
              "memory_address": f"{service['spec']['clusterIP']}:{port}", "billing_month": datetime.now(UTC).strftime("%Y-%m"),
              "runtime_commits": {"memory": args.memory_commit, "gateway": args.gateway_commit}}
    return frozen, db, {"topology": topology, "money": money}


def fixed_data():
    from agentruntime_learning.benchmarks import longmemeval as lme
    if hashlib.sha256(DATA.read_bytes()).hexdigest() != DATA_SHA256:
        raise PreflightError("LongMemEval source digest changed")
    selected = lme.sample(lme.load(DATA), limit=50, seed=0)
    old = json.loads((LEXICAL / "manifest.json").read_text())
    summary = json.loads((LEXICAL / "summary.json").read_text())
    if (not summary["valid"] or summary["completed"] != 50 or old["question_ids"] != [i.question_id for i in selected]
            or old["dataset_digest"] != lme._digest([asdict(i) for i in selected]) or old["top_k"] != 50 or old["ks"] != [5, 10, 30]):
        raise PreflightError("lexical baseline is not the frozen complete 50-question cohort")
    return selected


@contextmanager
def output_lock(path: Path):
    import fcntl
    path.mkdir(parents=True, exist_ok=True)
    with (path / ".launcher.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def run(args: argparse.Namespace) -> int:
    from agentruntime_learning.benchmarks import longmemeval as lme
    from agentruntime_memory import eval_retriever
    with output_lock(args.out):
        chosen = fixed_data()
        frozen, db, evidence = observe(args)
        estimate = lme.estimate_usd(chosen)
        code = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in (
            Path(__file__), Path(lme.__file__), Path(eval_retriever.__file__))}
        freeze = {"deployment": frozen, "runner_sha256": code, "data_sha256": DATA_SHA256, "seed": 0, "limit": 50,
                  "pipeline_timeout_s": 1200, "question_timeout_s": 1800, "approved_cap_usd": 20,
                  "historical_usd": PRIOR_MICROS / 1e6, "remaining_allowance_usd": CAP_MICROS / 1e6,
                  "admission_limit_usd": ADMIT_MICROS / 1e6, "estimated_embedding_usd": estimate,
                  "lexical_results_sha256": hashlib.sha256((LEXICAL / "results.jsonl").read_bytes()).hexdigest()}
        path = args.out / "freeze.json"
        if path.exists():
            if json.loads(path.read_text()) != freeze:
                raise PreflightError("frozen deployment or experiment changed; use a new output directory")
        elif db["model_usage"]:
            raise PreflightError("fresh experiment tenant already has charged attempts; reconcile original output")
        write(args.out / "preflight.json", {"freeze": freeze, "money": evidence["money"], "ready": True})
        if args.command == "preflight":
            print(json.dumps({"ready": True, "estimate_usd": estimate, "prior_cost_usd": PRIOR_MICROS / 1e6}))
            return 0
        write(path, freeze)
        write(args.out / "topology.json", evidence["topology"])
        write(args.out / "ledger-before.json", db)
        env = dict(os.environ, AR_MEMORY_ADDRESS=frozen["memory_address"], AR_MEMORY_TOPOLOGY_EVIDENCE=str(args.out / "topology.json"),
                   AR_MEMORY_EVAL_TENANT=TENANT, AR_MEMORY_EVAL_WAIT_S="1200")
        # The established Memory internal authenticator signs per-request tenant
        # assertions. Task API ops JWT is not involved in this experiment.
        secret = kubectl("get", "secret", "agent-runtime-secrets", "-o", "jsonpath={.data.dev-signing-key}")
        env["AR_DEV_SIGNING_KEY"] = base64.b64decode(secret).decode()
        factory = eval_retriever.live_retriever
        def checked_factory(corpus):
            now, rows, state = observe(args, before_question=True)
            if now != frozen:
                raise PreflightError("deployment drift before question; no new writes admitted")
            with (args.out / "budget-observations.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": datetime.now(UTC).isoformat(), **state["money"], "queue_count": rows["queue_count"]}) + "\n")
                fh.flush(); os.fsync(fh.fileno())
            return factory(corpus, env=env)
        eval_retriever.live_retriever = checked_factory
        summary = None
        try:
            provenance = {"data_sha256": DATA_SHA256, "seed": 0, "limit": 50, "memory_tenant": TENANT,
                          "runtime_revision": args.memory_commit, "gateway_revision": args.gateway_commit,
                          "registry_revision": "platform/model_profile/" + MODEL + "@" + frozen["registry"]["profile"]["version"],
                          "registry_document_sha256": frozen["registry"]["profile"]["document_sha256"],
                          "memory_version": frozen["memory"]["images"][0], "freeze_sha256": digest(freeze),
                          "runner_sha256": code, "pipeline_timeout_s": 1200}
            summary = asyncio.run(lme.run(chosen, "agentruntime_memory.eval_retriever:live_retriever", args.out / "live",
                                         top_k=50, ks=(5, 10, 30), budget_usd=CAP_MICROS / 1e6, provenance=provenance, timeout_s=1800))
        finally:
            eval_retriever.live_retriever = factory
            env.pop("AR_DEV_SIGNING_KEY", None)
            # Preserve raw evidence, including unknown costs or partial cleanup.
            final = db_snapshot()
            write(args.out / "ledger-after.json", final)
        if summary is None:
            return 4
        rows = lme._read_rows(args.out / "live/results.jsonl")
        all_ids = sorted({mid for row in rows.values() for mid in row.get("lifecycle", {}).get("submitted_ids", [])})
        remaining = []
        for i in range(0, len(all_ids), 200):
            remaining.extend(db_snapshot(all_ids[i:i + 200])["remaining_ids"])
        after, final, state = observe(args)
        cleanup = {"submitted_ids": len(all_ids), "remaining_ids": remaining, "queue_count": final["queue_count"],
                   "journal_cleanup_complete": all(row.get("cleanup_complete") for row in rows.values())}
        cleanup["complete"] = not remaining and not cleanup["queue_count"] and cleanup["journal_cleanup_complete"]
        write(args.out / "cleanup.json", cleanup)
        summary["actual_cost_usd"] = state["money"]["actual_usd"]
        summary["cost_source"] = "isolated tenant model_usage; every provider attempt"
        summary["valid"] = summary["valid"] and cleanup["complete"] and after == frozen
        write(args.out / "live/summary.json", summary)
        comparison = lme.compare(LEXICAL, args.out / "live")
        write(args.out / "comparison.json", comparison)
        write(args.out / "costs.json", state["money"])
        (args.out / "report.md").write_text(lme.report(summary) + "\n" + json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"completed": summary["completed"], "valid": summary["valid"], "comparison": comparison, "costs": state["money"]}))
        return 0 if comparison["passed"] else 4


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "run"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--memory-commit", required=True)
    parser.add_argument("--gateway-commit", required=True)
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (PreflightError, OSError, ValueError) as exc:
        # Error messages above contain only fixed operational codes, never RPC
        # payloads, raw kubectl output or model input.
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
