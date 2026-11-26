"""Run the predeclared quota or infrastructure recovery cases from the original Mini 50."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import threading

from run_paired_arm import DATA_SHA, PROFILE, PreparedRuntime, sb, sha, token_from_env
from run_routing_pilot import locked_output, resource, ROLES


class RecoveryClient(PreparedRuntime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop_path = self.out / "quota-stop.json"

    def create_workspace_operation(self, *args, **kwargs):
        if self.stop_path.exists():
            print("Provider quota guard halted new workspaces", file=sys.stderr)
            raise SystemExit(75)
        return super().create_workspace_operation(*args, **kwargs)

    def submit(self, **kwargs):
        if self.stop_path.exists():
            print("Provider quota guard halted submissions", file=sys.stderr)
            raise SystemExit(75)
        return super().submit(**kwargs)


def monitor(client, end, started):
    # Errors are gateway-scoped and lack task IDs. Trip conservatively only on
    # consecutive quota refusals from this experiment's dedicated fixed gateway.
    cancel_sent = set()
    while not end.wait(8):
        recs = sb.read_records(client.out)
        active = [r["task_id"] for r in recs.values() if r.get("task_id") and not r.get("task_finished")]
        if not active:
            continue
        if not client.stop_path.exists():
            try:
                logs = subprocess.run(["kubectl", "-n", "agent-runtime", "logs", "deploy/mini50-1121-gw",
                                       "--since-time=" + started, "--tail=1500"], text=True, capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if logs.returncode:
                continue
            refused = 0
            for line in logs.stdout.splitlines():
                if "profile=" + PROFILE + " " in line and "code=insufficient_user_quota" in line:
                    refused += 1
                elif line.startswith("{"):
                    row = json.loads(line)
                    if row.get("status") == "ok" and row.get("model_profile_id") == PROFILE:
                        refused = 0
            if refused < 3:
                continue
            client.stop_path.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                "gateway_scope": "mini50-1121-gw", "code": "insufficient_user_quota",
                "consecutive_refusals": refused, "active_in_arm": active}) + "\n")
        for task_id in active:
            if task_id not in cancel_sent:
                try:
                    client.cancel(task_id, "provider_insufficient_user_quota")
                    cancel_sent.add(task_id)
                except Exception as e:
                    print("Quota guard cancel pending", task_id, type(e).__name__, flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("arm", choices=["a", "b"])
    p.add_argument("--recovery", choices=("quota", "infrastructure"), default="quota")
    for name in ("archive", "dataset", "out", "prewarm", "freeze"):
        p.add_argument("--" + name, required=True, type=Path)
    a = p.parse_args()
    archive = json.loads(a.archive.read_text())["records"]
    source, = [k for k in archive if k.startswith("mini50-gpt62-formal-" + a.arm + "-")]
    quota = a.recovery == "quota"
    reasons = {"insufficient_user_quota"} if quota else {"prewarm_before_submission", "object_store_unavailable"}
    chosen = {r["instance_id"]: r["task_id"] for r in archive[source]["rows"]
              if r["reason"] in reasons and r["had_patch"] is False}
    ids = sorted(chosen)
    counts = {"a": 9, "b": 10} if quota else {"a": 4, "b": 3}
    if len(ids) != counts[a.arm] or sha(a.dataset) != DATA_SHA:
        raise ValueError("Input differs from the frozen recovery cohort")
    frozen = json.loads(a.freeze.read_text())
    prefix = "mini50-1121-" + a.arm
    gw = resource("deploy", "mini50-1121-gw")["spec"]["template"]["spec"]
    route_volume = next(v for v in gw["volumes"] if v["name"] == "route-lock")
    route_blob = resource("cm", route_volume["configMap"]["name"])["data"]["overrides.json"]
    policy = json.loads(route_blob)["tenants"]["swebench"]
    if any(policy.get(role) != {"profile": PROFILE, "tier": "strong@medium"} for role in ROLES):
        raise RuntimeError("The original fixed route differs")
    api_ip = resource("svc", prefix + "-api")["spec"]["clusterIP"]
    shared_ip = resource("svc", "ar-agent-runtime-task-service")["spec"]["clusterIP"]
    selection = {"dataset_name": "SWE-bench/SWE-bench_Verified", "dataset_split": "test",
                 "instances_sha256": DATA_SHA, "population_count": 500, "ids": True,
                 "source_mini_run": source, "previous_attempts": chosen,
                 "method_description": ("Prior Mini empty patches with an archived insufficient_user_quota error" if quota
                                        else "Original Mini cases without a verdict after prewarm or object-storage failure")}
    settings = sb.Settings(run_id=a.out.name, model_name=f"runtime-{a.arm}-gpt-6.2-sol-{a.recovery}-retry",
                           budget_usd=3*len(ids), task_usd_limit=3, concurrency=1,
                           task_timeout_s=3600, poll_s=10, hints=False, environment="official")
    protocol = {"mode": "second_attempt_quota_recovery" if quota else "missing_verdict_recovery",
                "arm": a.arm, "ids": ids, "profile": PROFILE,
                "tier": "strong@medium", "runtime_tree": frozen["baseline_tree" if a.arm == "a" else "candidate_tree"],
                "original_tasks": chosen, "price_scope": "Gateway reported model cost", "usd_per_task": 3,
                "timeout_seconds": 3600, "concurrency": 1, "prewarm_sha256": sha(a.prewarm)}
    with locked_output(a.out):
        path = a.out / "recovery-protocol.json"
        if path.exists() and json.loads(path.read_text()) != protocol:
            raise ValueError("Saved protocol differs")
        path.write_text(json.dumps(protocol, indent=2) + "\n")
        client = RecoveryClient(f"http://{api_ip}:8080", token_from_env(), a.out, a.prewarm,
                                {sb.workspace_id(a.out.name, i): i for i in ids}, diff_api=f"http://{shared_ip}:8080")
        done = threading.Event()
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        watch = threading.Thread(target=monitor, args=(client, done, started), daemon=True)
        watch.start()
        try:
            summary = sb.run(client, sb.select(sb.load_instances(a.dataset), ids=ids), a.out, settings,
                             selection=selection)
        except getattr(sb, "ProviderQuotaPaused", ()) as err:
            print(str(err), file=sys.stderr)
            return 75
        finally:
            done.set()
            watch.join(timeout=30)
    print(summary, flush=True)
    return int(bool(summary.errors or summary.not_run))


if __name__ == "__main__":
    raise SystemExit(main())
