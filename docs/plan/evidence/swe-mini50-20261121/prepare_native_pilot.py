"""Create the native-routing pilot from the existing isolated candidate deployments."""
import argparse
import json
import subprocess

NAMESPACE = "agent-runtime"
EXPERIMENT = "route2-1122-native"
TEMPORAL = "ar-route2-20261122-native"
BASE = "mini50-1121"
OWNER = "agentruntime.io/experiment"
ROLE = "agentruntime.io/role"
ARM = "agentruntime.io/arm"
POOLS = ("task", "workspace", "decision", "program", "model")


def kub(*args):
    return subprocess.check_output(["kubectl", "-n", NAMESPACE, *args], text=True)


def get(kind, name):
    return json.loads(kub("get", kind, name, "-o", "json"))


def resources(experiment=EXPERIMENT, temporal=TEMPORAL):
    docs = []
    for role in ("gw", "api", *POOLS):
        old_name = BASE + ("-gw" if role == "gw" else "-b-" + role)
        name = experiment + "-" + role
        old = get("deploy", old_name)
        if old["metadata"]["labels"].get(OWNER) != BASE or old["status"].get("availableReplicas") != 1:
            raise RuntimeError(f"Source workload is unavailable: {old_name}")
        meta = {"name": name, "namespace": NAMESPACE, "labels": {OWNER: experiment}}
        spec = old["spec"]
        template = spec["template"]
        selector = spec["selector"]["matchLabels"]
        selector.update({OWNER: experiment, ARM: "native"})
        labels = template["metadata"]["labels"]
        labels.update(selector)
        if role in POOLS:
            labels["agentruntime.io/pool"] = name
        else:
            labels["app.kubernetes.io/instance"] = experiment
        pod = template["spec"]
        container, = pod["containers"]
        replacements = {
            "AR_TEMPORAL_NAMESPACE": temporal,
            "AR_MODEL_GATEWAY_ADDRESS": experiment + "-gw:8091",
            "AR_TASK_SERVICE_INTERNAL_URL": "http://" + experiment + "-api:8097",
            "AR_PUBLIC_BASE_URL": "http://" + experiment + "-api:8080",
            "OTEL_SERVICE_VERSION": name,
        }
        for e in container["env"]:
            if e["name"] in replacements:
                e["value"] = replacements[e["name"]]
        if role == "gw":
            container["env"] = [e for e in container["env"] if e["name"] != "AR_MODEL_ROUTING_OVERRIDES_FILE"]
            container["volumeMounts"] = [v for v in container["volumeMounts"] if v["name"] != "route-lock"]
            pod["volumes"] = [v for v in pod["volumes"] if v["name"] != "route-lock"]
        docs.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta, "spec": spec})
        if role not in ("gw", "api"):
            continue
        svc = get("svc", old_name)["spec"]
        if svc["type"] != "ClusterIP":
            raise ValueError("Expected private evaluation services")
        docs.append({"apiVersion": "v1", "kind": "Service", "metadata": meta, "spec": {
            "type": "ClusterIP", "ports": svc["ports"], "selector": selector}})
        policy = get("networkpolicy", old_name)["spec"]
        policy["podSelector"] = {"matchLabels": selector}
        for rule in policy["ingress"]:
            for source in rule["from"]:
                peers = source["podSelector"]["matchLabels"]
                peers.update({OWNER: experiment, ARM: "native"})
        docs.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": meta, "spec": policy})
    return docs


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("check", "create", "cleanup"))
    action = p.parse_args().action
    if action == "cleanup":
        subprocess.run(["kubectl", "-n", NAMESPACE, "delete", "deploy,svc,netpol", "-l", OWNER + "=" + EXPERIMENT],
                       check=True)
        return
    docs = resources()
    # Create (rather than replace) protects older runs if names collide.
    docs.sort(key=lambda d: {"NetworkPolicy": 0, "Service": 1, "Deployment": 2}[d["kind"]])
    manifest = json.dumps({"apiVersion": "v1", "kind": "List", "items": docs})
    create = ["kubectl", "-n", NAMESPACE, "create", "-f", "-"]
    subprocess.run(create + ["--dry-run=server"], input=manifest, text=True, check=True)
    if action == "create":
        temporal = ["kubectl", "-n", "data", "exec", "deploy/temporal-admintools", "--", "temporal",
                    "operator", "namespace"]
        dest = ["--namespace", TEMPORAL, "--address", "temporal-frontend:7233"]
        if subprocess.run(temporal + ["describe", *dest], capture_output=True).returncode:
            subprocess.run(temporal + ["create", *dest, "--retention", "7d"], check=True)
        subprocess.run(create, input=manifest, text=True, check=True)


if __name__ == "__main__":
    main()
