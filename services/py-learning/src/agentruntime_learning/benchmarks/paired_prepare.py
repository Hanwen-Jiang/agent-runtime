"""Read-only Kubernetes/Registry observations and reproducible paired plans.

scaffold reads a saved manifest and makes an unobserved collector specification.
observe uses kubectl GET and read-only RLS database transactions, never exec/apply.
prepare only accepts independently observed off/rules receipts. No Task or model
is called by any command in this module.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agentruntime_learning.benchmarks import paired_completion as pc
from agentruntime_learning.benchmarks import provenance as pv

KIND = "paired-live-observation-v1"
NAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,252}\Z")


def write_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def kube(namespace: str, resource: str, name: str = "", *, selector: str = "", metadata_only: bool = False) -> Any:
    if not NAME.fullmatch(namespace) or (name and not NAME.fullmatch(name)):
        raise ValueError("invalid Kubernetes resource identity")
    if resource not in {"deployment", "pods", "configmap", "secret"}:
        raise ValueError("collector only supports read-only resource GET")
    argv = ["kubectl", "-n", namespace, "get", resource]
    if name:
        argv.append(name)
    if selector:
        argv += ["-l", selector]
    argv += ["-o", "jsonpath={.metadata.resourceVersion}" if metadata_only else "json", "--request-timeout=30s"]
    if not metadata_only:
        argv.append("--show-managed-fields=true")
    result = subprocess.run(argv, capture_output=True, timeout=40, check=False)
    if result.returncode:
        raise RuntimeError(f"read-only Kubernetes {resource} observation failed; credentials/stderr omitted")
    return result.stdout.decode().strip() if metadata_only else json.loads(result.stdout)


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def literal_env(spec: dict[str, Any]) -> dict[str, str]:
    out = {}
    for container in spec.get("containers", []):
        for env in container.get("env", []):
            if "value" not in env:
                continue
            name, value = env["name"], str(env["value"])
            # Paths and names referencing a secret are not secret values.
            if re.search(r"TOKEN|PASSWORD|SECRET|SIGNING_KEY|API_KEY|RELATIONAL_DSN", name) and not name.endswith(("_FILE", "_NAME", "_DIR")):
                raise ValueError("inline sensitive environment value cannot enter an observation")
            parsed = urlsplit(value)
            if parsed.username or parsed.password:
                raise ValueError("credential-bearing URL cannot enter an observation")
            if name in out and out[name] != value:
                raise ValueError("inconsistent environment across containers")
            out[name] = value
    return out


def deployment(namespace: str, name: str, get=kube) -> tuple[dict[str, Any], dict[str, Any]]:
    item = get(namespace, "deployment", name)
    spec = item["spec"]["template"]["spec"]
    literal_env(spec)  # Reject sensitive literals before retaining any source.
    wanted = int(item["spec"].get("replicas", 1))
    status = item.get("status", {})
    if wanted < 1 or int(status.get("observedGeneration", 0)) != int(item["metadata"]["generation"]):
        raise ValueError("deployment generation is not observed")
    if any(int(status.get(k, 0)) != wanted for k in ("updatedReplicas", "readyReplicas", "availableReplicas")):
        raise ValueError("deployment rollout is not complete")
    labels = item["spec"]["selector"]["matchLabels"]
    selected = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    pods = get(namespace, "pods", selector=selected)["items"]
    pods = [p for p in pods if not p.get("metadata", {}).get("deletionTimestamp")]
    if len(pods) != wanted:
        raise ValueError("deployment pod count differs from its stable rollout")
    images: dict[str, set[str]] = {}
    starts = []
    for pod in pods:
        # Includes actual pod environment, so a stale ReplicaSet cannot qualify.
        for container in spec["containers"]:
            actual = next((c for c in pod["spec"]["containers"] if c["name"] == container["name"]), None)
            if actual is None or any(actual.get(k) != container.get(k) for k in ("image", "args", "env", "envFrom")):
                raise ValueError("pod does not match the observed deployment template")
        for state in pod.get("status", {}).get("containerStatuses", []):
            if not state.get("ready") or "running" not in state.get("state", {}):
                raise ValueError("pod container is not ready and running")
            image = str(state.get("imageID", ""))
            if not re.search(r"sha256:[0-9a-f]{64}\Z", image):
                raise ValueError("actual running image digest is missing")
            images.setdefault(state["name"], set()).add(image)
            starts.append(timestamp(state["state"]["running"]["startedAt"]))
    if not starts or any(len(v) != 1 for v in images.values()) or set(images) != {c["name"] for c in spec["containers"]}:
        raise ValueError("replicas do not run one immutable image set")
    maps, secrets = {}, set()
    for volume in spec.get("volumes", []):
        if "configMap" in volume:
            cmname = volume["configMap"]["name"]
            cm = get(namespace, "configmap", cmname)
            # Gateway profiles are loaded once. Require last observed CM update
            # before all container starts; mounted new bytes alone are insufficient.
            updates = [timestamp(x["time"]) for x in cm["metadata"].get("managedFields", []) if x.get("time")]
            if not updates:
                raise ValueError("ConfigMap update time is unavailable; cannot attest startup configuration")
            if max(updates) > min(starts):
                raise ValueError("ConfigMap changed after process start; roll out before freezing")
            maps[cmname] = cm.get("data", {})
        if "secret" in volume:
            secrets.add(volume["secret"]["secretName"])
    for container in spec["containers"]:
        if container.get("envFrom"):
            raise ValueError("resolve envFrom into explicit observed references before freezing")
        for env in container.get("env", []):
            source = env.get("valueFrom", {})
            if "secretKeyRef" in source:
                secrets.add(source["secretKeyRef"]["name"])
            elif source and "fieldRef" not in source:
                raise ValueError("unobserved dynamic environment reference")
    # Never fetch Secret data. A rotation is still detected through metadata.
    secret_versions = {n: get(namespace, "secret", n, metadata_only=True) for n in sorted(secrets)}
    if any(not v for v in secret_versions.values()):
        raise ValueError("Secret metadata version is unavailable")
    source = {"spec": spec, "image_ids": {k: next(iter(v)) for k, v in images.items()},
              "configmap_sha256": {k: pv.digest(v) for k, v in sorted(maps.items())}, "secret_versions": secret_versions}
    receipt = {"deployment_uid": item["metadata"]["uid"], "generation": item["metadata"]["generation"],
               "resource_version": item["metadata"]["resourceVersion"], "container_started_at": sorted(s.isoformat() for s in starts)}
    return {"source": source, "configmaps": maps}, receipt


async def database(tenant_id: str, *, memory_enabled: bool) -> dict[str, Any]:
    from agentruntime_core.adapters.postgres import PostgresRelationalStoreProvider
    from agentruntime_core.tenancy import TenantContext
    dsn = os.environ.get("AR_RELATIONAL_DSN", "")
    if not dsn:
        raise ValueError("AR_RELATIONAL_DSN is required for read-only Registry/Memory observation")
    store = await PostgresRelationalStoreProvider.connect(dsn)
    try:
        scopes = [("platform", store.bind_platform("paired-read-only-observation")),
                  (tenant_id, store.bind(TenantContext(tenant_id=tenant_id)))]
        profiles, policy, memory = [], None, []
        for label, bound in scopes:
            async with bound.transaction(read_only=True) as tx:
                if label == "platform":
                    profiles = [dict(r) for r in await tx.query("SELECT name, version, document FROM registry_entries WHERE kind = 4 AND state = 2 ORDER BY name")]
                rows = await tx.query("SELECT version, document FROM registry_entries WHERE kind = 5 AND name = '' AND state = 2")
                if rows:
                    policy = {"scope": label, "revision": "v" + str(rows[0]["version"]), "document": json.loads(rows[0]["document"])}
                if memory_enabled:
                    if await tx.query("SELECT seq FROM memory_queue LIMIT 1"):
                        raise ValueError("Memory has queued writes; freeze its corpus before observing")
                    rows = await tx.query("SELECT id, level, acl, kind, status, source, trust, valid_from, valid_to, superseded_by, families, tools, content_hash, signature, version FROM memory_items ORDER BY id LIMIT 100001")
                    if len(rows) > 100000:
                        raise ValueError("Memory corpus exceeds safe observation size")
                    memory.append({"scope": label, "corpus_sha256": pv.digest([dict(r) for r in rows]), "count": len(rows)})
        if policy is None:
            raise ValueError("no effective approved Registry tenant policy")
        return {"profiles": profiles, "tenant_policy": policy,
                "memory_corpus_sha256": pv.digest(memory if memory_enabled else {"enabled": False}), "memory_scopes": memory}
    finally:
        await store.aclose()


def ensure_revision(source: dict[str, Any], revision: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("runtime revision must be a full source commit")
    for container in source["spec"]["containers"]:
        tag = container["image"].rsplit(":", 1)[-1]
        if not re.fullmatch(r"[0-9a-f]{7,40}", tag) or not revision.startswith(tag):
            raise ValueError("running worker/Gateway image tag does not identify the requested commit")


def materialize(spec: dict[str, Any], observed: dict[str, Any], db: dict[str, Any]) -> dict[str, Any]:
    workers = {name: observed[name]["source"] for name in spec["workers"]}
    for source in workers.values():
        ensure_revision(source, spec["runtime_commit"])
    worker = pv.worker_configuration(workers)
    policies = {entry["value"] for entry in worker["removed"]}
    if len(policies) != 1:
        raise ValueError("worker pools disagree about the sub-agent policy")
    gateway = observed[spec["gateway"]]
    ensure_revision(gateway["source"], spec["runtime_commit"])
    env = literal_env(gateway["source"]["spec"])
    if env.get("AR_MODEL_PROFILES_REQUIRE_REGISTRY") != "true" or env.get("AR_MODEL_PROVIDER") != "openai_compat":
        raise ValueError("Gateway must enforce real Registry profiles and use the approved real provider")
    maps = gateway["configmaps"]
    catalogs = [json.loads(v["model-profiles.json"]) for v in maps.values() if "model-profiles.json" in v]
    if len(catalogs) != 1:
        raise ValueError("exactly one mounted Gateway model catalog is required")
    catalog = catalogs[0]
    actual = {p["id"]: p for p in catalog["profiles"]}
    registry, prices = {}, {}
    for row in db["profiles"]:
        name = row["name"]
        if name not in actual:
            continue
        identity = {"scope": "platform", "version": "v" + str(row["version"]), "sha256": hashlib.sha256(row["document"].encode()).hexdigest()}
        if catalog.get("registry_revisions", {}).get(name) != identity or json.loads(row["document"]) != actual[name]:
            raise ValueError("Gateway catalog differs from the actual immutable Registry document")
        registry[name] = {"scope": "platform", "revision": identity["version"], "document_sha256": identity["sha256"]}
        prices[name] = actual[name]["cost"]
    used = set(spec["models"].values()) | {spec["semantic_judge_profile"]}
    if not used <= registry.keys():
        raise ValueError("a frozen subject/judge profile is not loaded from its Registry version")
    files = [json.loads(v["tenant-policies.json"]) for v in maps.values() if "tenant-policies.json" in v]
    if len(files) > 1:
        raise ValueError("ambiguous Gateway policy files")
    file = files[0] if files else {}
    own = file.get("tenants", {}).get(spec["tenant_id"], {})
    base = file.get("default", {})
    policy = db["tenant_policy"]["document"]
    # Gateway uses the mounted per-tenant allowlist then the default allowlist.
    allowlist = own.get("model_allowlist", base.get("model_allowlist"))
    if allowlist is None or not set(spec["models"].values()) <= set(allowlist):
        raise ValueError("explicit Gateway allowlist must include each frozen subject profile")
    limits = {**base.get("model_budget", {}), **policy.get("model_budget", {}), **own.get("model_budget", {})}
    for key in ("daily_usd", "monthly_usd"):
        pc.finite(limits.get(key, -1), "observed Gateway " + key, positive=True)
    enabled = {bool(literal_env(s["spec"]).get("AR_MEMORY_ADDRESS")) for s in workers.values()}
    if len(enabled) != 1:
        raise ValueError("Memory configuration differs across worker pools")
    memory = observed[spec["memory"]]["source"] if True in enabled else {"enabled": False}
    provenance = {"runtime_commit": spec["runtime_commit"], "models": spec["models"], "registry": {k: registry[k] for k in sorted(used)},
                  "gateway_config_sha256": pv.digest(gateway["source"]), "worker_configuration": worker,
                  "worker_config_sha256": worker["normalized_sha256"], "tenant_policy_sha256": pv.digest({"registry": db["tenant_policy"], "file": file}),
                  "memory": {"revision": pv.digest(memory), "corpus_sha256": db["memory_corpus_sha256"], "enabled": True in enabled},
                  "sub_agent_policy": next(iter(policies)), "scoring_version": pc.VERSION,
                  "gateway_budget_limits": limits, "semantic_judge_profile": spec["semantic_judge_profile"]}
    return {"provenance": pv.validate(provenance), "prices": {k: prices[k] for k in sorted(used)}, "memory_scopes": db["memory_scopes"]}


def observe(spec_path: Path, *, get=kube, read_database=None) -> dict[str, Any]:
    spec = json.loads(spec_path.read_text())
    namespace = spec["namespace"]
    names = list(dict.fromkeys([*spec["workers"], spec["gateway"], spec["memory"]]))
    observed, receipts = {}, {}
    for name in names:
        observed[name], receipts[name] = deployment(namespace, name, get)
    enabled = any(literal_env(observed[n]["source"]["spec"]).get("AR_MEMORY_ADDRESS") for n in spec["workers"])
    db = read_database(spec["tenant_id"], memory_enabled=enabled) if read_database else asyncio.run(database(spec["tenant_id"], memory_enabled=enabled))
    result = materialize(spec, observed, db)
    return {"kind": KIND, "observed_at": datetime.now(timezone.utc).isoformat(), "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "spec_path": str(spec_path.resolve()), "spec_sha256": pv.digest(spec), "tenant_id": spec["tenant_id"],
            "task_api": spec["task_api"], "deployment_receipts": receipts, **result}


def price_estimate(settings: dict[str, Any], prices: dict[str, Any], models: dict[str, str], judge_profile: str, count: int) -> dict[str, Any]:
    total = 0.0
    lines = []
    usage = settings["expected_usage_per_task"]
    for arm in ("off", "rules"):
        for role, row in usage[arm].items():
            price = prices[models[role]]
            calls = pc.finite(row["calls"], "expected calls")
            cost = calls * (pc.finite(row["input_tokens"], "input tokens") * float(price["in_per_m"]) + pc.finite(row["output_tokens"], "output tokens") * float(price["out_per_m"])) / 1e6
            total += count * cost
            lines.append({"arm": arm, "role": role, "profile": models[role], "per_task_usd": cost, "assumptions": row, "price": price})
    judge = settings["expected_judge_usage"]
    price = prices[judge_profile]
    judge_cost = pc.finite(judge["calls"], "expected judge calls") * (pc.finite(judge["input_tokens"], "judge input") * float(price["in_per_m"]) + pc.finite(judge["output_tokens"], "judge output") * float(price["out_per_m"])) / 1e6
    jev = settings["expected_jev_usage"]
    if not jev.get("price_basis"):
        raise ValueError("Jev pricing assumption needs its approved/account evidence source")
    if jev.get("usd_per_million_input_tokens") != .042:
        raise ValueError("Jev price must use the verified $0.042/M input token basis")
    jev_cost = pc.finite(jev["max_calls"], "Jev calls") * pc.finite(jev["input_tokens_per_call"], "Jev input tokens") * .042 / 1e6
    return {"model_tasks_usd": total, "semantic_judge_usd": judge_cost, "external_jev_usd": jev_cost,
            "total_usd": total + judge_cost + jev_cost, "task_lines": lines, "judge_assumptions": judge, "jev_assumptions": jev,
            "is_hard_upper_bound": False, "source": "observed Registry prices and preregistered token/call assumptions"}


def prepare(off: dict[str, Any], rules: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    from agentruntime_learning.benchmarks.paired import estimate
    for arm, receipt in (("off", off), ("rules", rules)):
        if receipt.get("kind") != KIND or not receipt.get("deployment_receipts") or not receipt.get("observed_at"):
            raise ValueError("prepare requires real observed deployment receipts, not a manifest candidate")
        if receipt["collector_sha256"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
            raise ValueError("observation collector changed; take a new read-only observation")
        if pv.validate(receipt["provenance"])["sub_agent_policy"] != arm:
            raise ValueError("observation arm mismatch")
        spec_path = Path(receipt["spec_path"])
        if pv.digest(json.loads(spec_path.read_text())) != receipt["spec_sha256"]:
            raise ValueError("observation specification changed")
    if off["tenant_id"] != rules["tenant_id"] or off["prices"] != rules["prices"]:
        raise ValueError("both arms must use the same tenant and prices")
    if not pv.comparable(off["provenance"], rules["provenance"], paired=True):
        raise ValueError("observed paired configurations differ beyond the arm switch")
    cases = pc.completion_cases()
    priced = price_estimate(settings, off["prices"], off["provenance"]["models"], off["provenance"]["semantic_judge_profile"], len(cases))
    plan = {k: settings[k] for k in ("run_id", "seed", "budget_usd", "task_usd_limit", "cost_reserves", "thresholds")}
    plan.update(tenant_id=off["tenant_id"], cases=cases, historical_runs=settings.get("historical_runs", []),
                prior_external_cost_usd=settings.get("prior_external_cost_usd", 0), task_timeout_s=settings.get("task_timeout_s", 900),
                arms={arm: {"task_api": r["task_api"], "snapshot_command": [sys.executable, "-m", __name__, "observe", "--spec", r["spec_path"], "--provenance-only"],
                            "provenance": r["provenance"]} for arm, r in (("off", off), ("rules", rules))})
    plan["completion"] = {"version": pc.VERSION, "rubric": pc.rubric(), "cases_sha256": pv.digest(cases),
                          "original_message_sha256": hashlib.sha256(pc.ORIGINAL_MESSAGE.encode()).hexdigest(), "price_estimate": priced,
                          "observation_receipts": {"off": off, "rules": rules}}
    pc.validate_completion(plan)
    spending = estimate(plan)
    if not spending["fits"] or priced["model_tasks_usd"] > spending["task_gateway_budget_usd"]:
        raise ValueError("projected experiment does not fit remaining shared budget")
    if priced["semantic_judge_usd"] > plan["cost_reserves"]["semantic_judge_usd"] or priced["external_jev_usd"] > plan["cost_reserves"]["external_jev_usd"]:
        raise ValueError("judge/Jev estimates exceed their preregistered reserves")
    if max(off["provenance"]["gateway_budget_limits"].values()) > spending["task_gateway_budget_usd"]:
        raise ValueError("observed isolated Gateway caps exceed the Task allocation after judge/Jev reserves")
    return plan


def scaffold(manifest: Path, models: Path, runtime_commit: str, tenant: str, task_api: str, judge_profile: str) -> dict[str, Any]:
    import yaml
    docs = [x for x in yaml.safe_load_all(manifest.read_text()) if isinstance(x, dict) and x.get("kind") == "Deployment"]
    def names(component):
        return [d["metadata"]["name"] for d in docs if d["metadata"].get("labels", {}).get("app.kubernetes.io/component") == component]
    if not names("worker") or len(names("model-gateway")) != 1 or len(names("memory")) != 1:
        raise ValueError("saved manifest must contain worker, model-gateway and memory deployments")
    namespaces = {d["metadata"].get("namespace", "agent-runtime") for d in docs}
    if len(namespaces) != 1:
        raise ValueError("saved manifest namespaces differ")
    return {"namespace": namespaces.pop(), "workers": sorted(names("worker")), "gateway": names("model-gateway")[0], "memory": names("memory")[0],
            "runtime_commit": runtime_commit, "tenant_id": tenant, "task_api": task_api,
            "models": json.loads(models.read_text()), "semantic_judge_profile": judge_profile,
            "source_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(), "observed": False}


async def scope_ledger(tenant_id: str) -> list[dict[str, Any]]:
    from agentruntime_core.adapters.postgres import PostgresRelationalStoreProvider
    from agentruntime_core.tenancy import TenantContext
    store = await PostgresRelationalStoreProvider.connect(os.environ["AR_RELATIONAL_DSN"])
    try:
        async with store.bind(TenantContext(tenant_id=tenant_id)).transaction(read_only=True) as tx:
            return [dict(r) for r in await tx.query("SELECT reservation_id, task_id, node_id, model_profile_id, actual_usd_micros, registry_scope, registry_revision, registry_document_sha256 FROM model_usage ORDER BY reservation_id")]
    finally:
        await store.aclose()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sc = sub.add_parser("scaffold")
    for name in ("manifest", "models", "out"):
        sc.add_argument("--" + name, type=Path, required=True)
    for name in ("runtime-commit", "tenant", "task-api", "judge-profile"):
        sc.add_argument("--" + name, required=True)
    obs = sub.add_parser("observe")
    obs.add_argument("--spec", type=Path, required=True)
    obs.add_argument("--out", type=Path)
    obs.add_argument("--provenance-only", action="store_true")
    prep = sub.add_parser("prepare")
    for name in ("off", "rules", "settings", "out"):
        prep.add_argument("--" + name, type=Path, required=True)
    review = sub.add_parser("review-export")
    review.add_argument("--out", type=Path, required=True)
    review.add_argument("--destination", type=Path, required=True)
    ledger = sub.add_parser("scope-ledger")
    ledger.add_argument("--tenant", required=True)
    ledger.add_argument("--out", type=Path, required=True)
    report = sub.add_parser("completion-report")
    for name in ("out", "export", "review", "scopes", "destination"):
        report.add_argument("--" + name, type=Path, required=True)
    args = p.parse_args(argv)
    try:
        if args.command == "scaffold":
            result = scaffold(args.manifest, args.models, args.runtime_commit, args.tenant, args.task_api, args.judge_profile)
            write_new(args.out, result)
        elif args.command == "observe":
            result = observe(args.spec)
            if args.out:
                write_new(args.out, result)
            if args.provenance_only:
                result = result["provenance"]
        elif args.command == "prepare":
            result = prepare(*(json.loads(path.read_text()) for path in (args.off, args.rules, args.settings)))
            write_new(args.out, result)
        elif args.command == "scope-ledger":
            result = asyncio.run(scope_ledger(args.tenant))
            write_new(args.out, result)
        elif args.command == "completion-report":
            result = pc.completed_report(args.out, *(json.loads(p.read_text()) for p in (args.export, args.review, args.scopes)))
            write_new(args.destination, result)
        else:
            result = pc.review_export(args.out, args.destination)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # Kubernetes/SQL errors may contain credentials; never echo their text.
        print(json.dumps({"ready": False, "error": type(exc).__name__, "hint": "No model/Task was called. Check read-only permissions and the preregistered preparation requirements."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
