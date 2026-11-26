"""Retire only the seven C deployments; keep services, configuration, and Temporal sessions."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time

from completion_gate import CASES, RUNS, check

POOLS = ("api", "task", "workspace", "decision", "model", "program")
OWNED = [f"swe50-1123-c-{role}" for role in POOLS] + ["swe50-1123-sandbox"]
PROTECTED = [f"mini50-1121-{arm}-{role}" for arm in ("a", "b") for role in POOLS] + [
    "mini50-1121-gw",
    "ar-agent-runtime-sandbox-service",
    "ar-agent-runtime-model-gateway",
]
NAMESPACE = "ar-swe50-20261123-c"


def command(cmd):
    return subprocess.check_output(cmd, text=True)


def data(args):
    return json.loads(command(["kubectl", "-n", "agent-runtime", *args]))


def running_workflows():
    rows = json.loads(
        command(
            [
                "kubectl",
                "-n",
                "data",
                "exec",
                "deploy/temporal-admintools",
                "--",
                "temporal",
                "workflow",
                "list",
                "--namespace",
                NAMESPACE,
                "--address",
                "temporal-frontend:7233",
                "--query",
                "ExecutionStatus='Running'",
                "--limit",
                "1000",
                "--output",
                "json",
            ]
        )
    )
    counts = Counter(row["type"]["name"] for row in rows)
    if any(kind != "SessionWorkflow" for kind in counts):
        raise RuntimeError(f"C still has unfinished non-session workflows: {counts}")
    return dict(counts)


def workloads(names):
    rows = data(["get", "deployment", *names, "-o", "json"])["items"]
    if len(rows) != len(names):
        raise RuntimeError("Deployment inventory changed")
    return {
        row["metadata"]["name"]: {
            "uid": row["metadata"]["uid"],
            "generation": row["metadata"]["generation"],
            "replicas": row["spec"].get("replicas", 0),
            "ready": row["status"].get("readyReplicas", 0),
            "owner": row["metadata"]["labels"].get("agentruntime.io/tuning"),
            "spec_sha256": hashlib.sha256(
                json.dumps(row["spec"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        for row in rows
    }


def resource_uids(kind):
    names = command(
        [
            "kubectl",
            "-n",
            "agent-runtime",
            "get",
            kind,
            "-o",
            "jsonpath={range .items[*]}{.metadata.name}{' '}{.metadata.uid}{'\\n'}{end}",
        ]
    )
    return {k: v for line in names.splitlines() for k, v in [line.split()] if k.startswith("swe50-1123-")}


def main(quota):
    grades = [check(RUNS / name) for name in CASES]
    if not quota.is_file():
        raise RuntimeError("init-50 quota reconciliation receipt missing")
    proof = json.loads(quota.read_bytes())
    if proof.get("mode") != "apply" or len(proof.get("cases", [])) != 44:
        raise RuntimeError("init-50 quota reconciliation not complete")
    cache = Path(__file__).with_name("cache-final-verification.json")
    cache_proof = json.loads(cache.read_bytes())
    mirror = cache_proof["mirror"]
    registry = cache_proof["private_registry"]
    if mirror["buffer_mounted"] or mirror["records"] != mirror["restored_blobs_verified"]:
        raise RuntimeError("previously existing mirror cache still awaits restoration")
    if registry["authenticated_GET_verified"] != registry["records"]:
        raise RuntimeError("private-registry verification needs the C worker")
    protected_before = workloads(PROTECTED)
    before = workloads(OWNED)
    sessions = running_workflows()
    persistent = {kind: resource_uids(kind) for kind in ("svc", "cm", "networkpolicy")}
    for name in OWNED:
        row = before[name]
        if row["owner"] != "swe50-1123" or row["replicas"] not in (0, 1):
            raise RuntimeError("candidate deployment ownership or replica count changed")
        if row["replicas"] == 1:
            command(
                [
                    "kubectl",
                    "-n",
                    "agent-runtime",
                    "scale",
                    "deployment/" + name,
                    "--current-replicas=1",
                    "--replicas=0",
                ]
            )
    for _ in range(60):
        after = workloads(OWNED)
        pods = data(["get", "pods", "-l", "agentruntime.io/tuning=swe50-1123", "-o", "json"])["items"]
        if not pods and all(r["replicas"] == r["ready"] == 0 for r in after.values()):
            break
        time.sleep(2)
    else:
        raise RuntimeError("Candidate pods are still terminating")
    protected_after = workloads(PROTECTED)
    for name in PROTECTED:
        for field in ("uid", "generation", "spec_sha256", "replicas"):
            if protected_after[name][field] != protected_before[name][field]:
                raise RuntimeError(f"Protected workload changed during closeout: {name}/{field}")
    for kind, uids in persistent.items():
        if resource_uids(kind) != uids:
            raise RuntimeError(f"Persistent {kind} inventory changed")
    if any(before[n]["uid"] != after[n]["uid"] for n in OWNED):
        raise RuntimeError("candidate controller replaced")
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "runs": grades,
        "quota_receipt_sha256": hashlib.sha256(quota.read_bytes()).hexdigest(),
        "cache_receipt_sha256": hashlib.sha256(cache.read_bytes()).hexdigest(),
        "before": before,
        "after": after,
        "protected": protected_after,
        "protected_specs_unchanged": True,
        "kept_resources": persistent,
        "temporal_namespace": NAMESPACE,
        "idle_workflows_before_scale": sessions,
        "idle_workflows_after_scale": running_workflows(),
        "remaining_candidate_pods": len(pods),
    }


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--quota-receipt", type=Path, required=True)
    print(json.dumps(main(args.parse_args().quota_receipt), indent=2))
