"""Run a complete 50-task cohort on the isolated, frozen C Runtime."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import threading

from run_paired_arm import DATA_SHA, PROFILE, sb, sha, token_from_env
from run_quota_recovery import RecoveryClient, monitor
from run_routing_pilot import locked_output, resource, snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("selection", "ids", "freeze", "out", "prewarm"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--worker-prefix", default="swe50-1123-c")
    parser.add_argument("--dataset", type=Path, default=Path("/opt/swebench/data/verified.jsonl"))
    args = parser.parse_args()
    selection = json.loads(args.selection.read_text())
    ids = args.ids.read_text().splitlines()
    if len(ids) != len(set(ids)) or len(ids) != 50:
        raise ValueError("Expected the complete, unique cohort of 50")
    if sha(args.ids) != selection["cohort_file_sha256"] or sha(args.dataset) != DATA_SHA:
        raise ValueError("The input list or original Verified export changed")
    freeze = json.loads(args.freeze.read_text())
    conditions, api_ip, routes = snapshot(args.worker_prefix, freeze, "fixed")
    shared_ip = resource("svc", "ar-agent-runtime-task-service")["spec"]["clusterIP"]
    selection.update(instances_sha256=DATA_SHA, ids_file=str(args.ids), runtime_tree=conditions["runtime_tree"])
    label = "runtime-c-gpt-6.2-sol-fixed"
    settings = sb.Settings(run_id=args.out.name, model_name=label, budget_usd=150.0, task_usd_limit=3.0,
                           concurrency=args.concurrency, task_timeout_s=3600, poll_s=10,
                           hints=False, environment="official")
    protocol = {"run_id": args.out.name, "arm":"c", "model_label":label, "model_profile":PROFILE,
                "tier":"strong@medium", "runtime_tree":conditions["runtime_tree"], "conditions":conditions,
                "requested_routes":routes, "prewarm_sha256":sha(args.prewarm), "input":selection,
                "instance_ids":ids, "hints":False, "official_grader":True, "single_generation_attempt":True,
                "budget_usd":settings.budget_usd, "task_usd_limit":settings.task_usd_limit,
                "task_timeout_s":settings.task_timeout_s, "concurrency":settings.concurrency}
    selected = sb.select(sb.load_instances(args.dataset), ids=ids)
    if ids != [i.instance_id for i in selected]:
        raise ValueError("The adapter selected a different cohort or order")
    with locked_output(args.out):
        path = args.out / "paired-protocol.json"
        if path.exists() and json.loads(path.read_text()) != protocol:
            raise ValueError("Resume requires the original deployment and inputs")
        path.write_text(json.dumps(protocol, indent=2) + "\n")
        client = RecoveryClient(f"http://{api_ip}:8080", token_from_env(), args.out, args.prewarm,
                                {sb.workspace_id(args.out.name, i): i for i in ids},
                                diff_api=f"http://{shared_ip}:8080")
        end = threading.Event()
        start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        guard = threading.Thread(target=monitor, args=(client,end,start), daemon=True)
        guard.start()
        try:
            result = sb.run(client, selected, args.out, settings, selection=selection)
        except sb.ProviderQuotaPaused:
            return 75
        finally:
            end.set()
            guard.join(timeout=30)
    print(result, flush=True)
    return int(bool(result.errors or result.not_run))


if __name__ == "__main__":
    raise SystemExit(main())
