"""Run one side of a small SWE-bench fixed-vs-native routing comparison."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess

from run_paired_arm import DATA_SHA, PROFILE, PreparedRuntime, sb, sha, token_from_env

HERE = Path(__file__).resolve().parent
CASES = {"django__django-11490": "init50_only", "sphinx-doc__sphinx-8721": "mini50_only"}
CACHED_CASES = {"django__django-11885": "init50_and_mini50", "sphinx-doc__sphinx-8721": "mini50_only"}
ROLES = {"fetch", "transform", "plan", "route", "judge", "aggregate", "act", "gate", "deliver", "summary", "probe"}
POOLS = ("task", "decision", "model", "program", "workspace")
ROUTE_ENV = "AR_MODEL_ROUTING_OVERRIDES_FILE"


def kubectl(*args):
    return subprocess.check_output(["kubectl", "-n", "agent-runtime", *args], text=True)


def resource(kind, name):
    return json.loads(kubectl("get", kind, name, "-o", "json"))


def ready_pod(selector):
    label = ",".join(f"{k}={v}" for k, v in sorted(selector.items()))
    pods = json.loads(kubectl("get", "pods", "-l", label, "-o", "json"))["items"]
    ready = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")
             and pod["status"].get("phase") == "Running"
             and all(c["ready"] for c in pod["status"].get("containerStatuses", []))]
    if len(ready) != 1 or len(ready[0]["status"].get("containerStatuses", [])) != 1:
        raise ValueError(f"Expected one ready evaluation replica for {label}")
    return ready[0]


def env(pod):
    return {e["name"]: e.get("value") for e in pod["spec"]["containers"][0]["env"]}


def document(pod, path):
    """Read only the ConfigMap file actually selected by the running pod's mount."""
    for mount in pod["spec"]["containers"][0]["volumeMounts"]:
        relative = Path(path).relative_to(mount["mountPath"]) if Path(path).is_relative_to(mount["mountPath"]) else None
        if relative is None:
            continue
        volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
        ref = volumes[mount["name"]]["configMap"]
        cm = resource("cm", ref["name"])
        filename = mount.get("subPath", str(relative))
        entries = {i["path"]: i["key"] for i in ref.get("items", [])}
        return cm["data"][entries.get(filename, filename)]
    raise ValueError(f"Missing ConfigMap mount for {path}")


def digest(data):
    return hashlib.sha256(data.encode()).hexdigest()


def snapshot(prefix, freeze, mode):
    workers = {pool: ready_pod(resource("deploy", f"{prefix}-{pool}")["spec"]["selector"]["matchLabels"])
               for pool in POOLS}
    gateways = {env(p)["AR_MODEL_GATEWAY_ADDRESS"] for p in workers.values()}
    if len(gateways) != 1:
        raise ValueError("The five worker pools point at different gateways")
    gateway = ready_pod(resource("svc", gateways.pop().split(":")[0].split(".")[0])["spec"]["selector"])
    variables = env(gateway)
    routes = {}
    if variables.get(ROUTE_ENV):
        routes = json.loads(document(gateway, variables[ROUTE_ENV]))["tenants"].get("swebench", {})
    if mode == "fixed" and any(routes.get(role) != {"profile": PROFILE, "tier": "strong@medium"} for role in ROLES):
        raise ValueError("The fixed gateway is missing the GPT-6.2 override")
    if mode == "native" and routes:
        raise ValueError("The native gateway still forces routes for this tenant")
    api_service = resource("svc", prefix + "-api")
    api = ready_pod(api_service["spec"]["selector"])
    if {env(p)["AR_TEMPORAL_NAMESPACE"] for p in workers.values()} != {env(api)["AR_TEMPORAL_NAMESPACE"]}:
        raise ValueError("The API and worker pools use different Temporal namespaces")
    for pool, worker in workers.items():
        for source, expected in freeze["candidate_files"].items():
            path = "/app/services/py-worker/src/agentruntime_worker/" + source
            if digest(document(worker, path)) != expected:
                raise ValueError(f"{pool} does not mount the expected {source}")
    def image_id(pod):
        return pod["status"]["containerStatuses"][0]["imageID"]
    conditions = {
        "runtime_tree": freeze["candidate_tree"],
        "candidate_files": freeze["candidate_files"],
        "images": {key: image_id(pod) for key, pod in {**workers, "api": api, "gateway": gateway}.items()},
        "catalog_sha256": digest(document(gateway, variables["AR_MODEL_PROFILES_FILE"])),
        "policy_sha256": digest(document(gateway, variables["AR_DEV_TENANT_POLICIES_FILE"])),
    }
    return conditions, api_service["spec"]["clusterIP"], routes


@contextmanager
def locked_output(path):
    path.mkdir(parents=True, exist_ok=True)
    with (path / ".runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("fixed", "native"))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--prewarm", required=True, type=Path)
    parser.add_argument("--worker-prefix", default="mini50-1121-b")
    parser.add_argument("--freeze", type=Path, default=HERE / "runtime-arms" / "freeze.json")
    parser.add_argument("--dataset", type=Path, default=Path("/opt/swebench/data/verified.jsonl"))
    parser.add_argument("--reference", type=Path, help="Fixed arm's route-protocol.json")
    parser.add_argument("--cohort", choices=("original", "cached"), default="original",
                        help="The cached follow-up replaces 11490 after its recorded image-quota failure.")
    args = parser.parse_args()
    if args.mode == "native" and not args.reference:
        parser.error("native requires --reference from the fixed arm")
    if sha(args.dataset) != DATA_SHA or not args.prewarm.is_file():
        raise ValueError("Check the Verified input SHA and project-image prewarm helper")
    conditions, api_ip, routes = snapshot(args.worker_prefix, json.loads(args.freeze.read_text()), args.mode)
    cases = CASES if args.cohort == "original" else CACHED_CASES
    ids = list(cases)
    selection = {"dataset_name": "SWE-bench/SWE-bench_Verified", "dataset_split": "test",
                 "instances_sha256": DATA_SHA, "population_count": 500, "ids": True,
                 "ids_sha256": digest("\n".join(ids) + "\n"), "case_origins": cases}
    conditions.update(selection=selection, usd_per_task=3, seconds_per_task=3600, concurrency=1,
                      hints=False, environment="official", prewarm_sha256=sha(args.prewarm))
    if args.reference:
        ref = json.loads(args.reference.read_text())
        if ref["mode"] != "fixed" or ref["conditions"] != conditions or ref["run_id"] == args.out.name:
            raise ValueError("Routing pair conditions or run IDs are incompatible")
    protocol = {"run_id": args.out.name, "mode": args.mode, "conditions": conditions,
                "requested_routes": routes, "instance_ids": ids}
    instances = sb.select(sb.load_instances(args.dataset), ids=ids)
    settings = sb.Settings(run_id=args.out.name, model_name="candidate-" + args.mode + "-routing",
                           budget_usd=3.0 * len(ids), task_usd_limit=3.0, task_timeout_s=3600,
                           poll_s=10, concurrency=1, hints=False, environment="official")
    shared_ip = resource("svc", "ar-agent-runtime-task-service")["spec"]["clusterIP"]
    with locked_output(args.out):
        path = args.out / "route-protocol.json"
        if path.exists() and json.loads(path.read_text()) != protocol:
            raise ValueError("The saved run differs from this protocol")
        path.write_text(json.dumps(protocol, ensure_ascii=False, indent=2) + "\n")
        client = PreparedRuntime(f"http://{api_ip}:8080", token_from_env(), args.out, args.prewarm,
                                 {sb.workspace_id(args.out.name, iid): iid for iid in ids},
                                 diff_api=f"http://{shared_ip}:8080")
        result = sb.run(client, instances, args.out, settings, selection=selection)
    print(json.dumps({"run_id": args.out.name, "finished": result.finished, "errors": result.errors,
                      "not_run": result.not_run}))
    return int(bool(result.errors or result.not_run))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except sb.ProviderQuotaPaused:
        raise SystemExit(75) from None
