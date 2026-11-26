"""Create the scoped sandbox service shared by the two November 23 evaluation batches."""
import json
import subprocess
from copy import deepcopy

NS = "agent-runtime"
ORIGINAL = "ar-agent-runtime-sandbox-service"
BACKENDS = "ar-agent-runtime-sandbox-backends"
EVAL = "swe50-1123-sandbox"
EVAL_BACKENDS = EVAL + "-backends"
LABEL = "agentruntime.io/tuning"
VERSION = "swe50-1123"
POD_SELECTOR = {LABEL: VERSION, "app.kubernetes.io/component": "sandbox-service"}
OWNER = {LABEL: VERSION}

def kubectl(*cmd, data=None):
    return subprocess.check_output(["kubectl", "-n", NS, *cmd], input=data, text=True)

def get(kind, name):
    return json.loads(kubectl("get", kind, name, "-o", "json"))

def body(api, kind, name, **fields):
    return {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": NS, "labels": OWNER}, **fields}

def apply_owned(doc):
    name = doc["metadata"]["name"]
    old = kubectl("get", doc["kind"], name, "--ignore-not-found", "-o", "json")
    if old and json.loads(old)["metadata"].get("labels", {}).get(LABEL) != VERSION:
        raise ValueError("unrelated resource exists: " + name)
    kubectl("apply", "-f", "-", data=json.dumps(doc))

def main():
    config = get("cm", BACKENDS)["data"]
    parsed = json.loads(config["backends.json"])
    assert parsed["environments"]["limits"]["max_images"] == 50
    parsed["environments"]["limits"]["max_images"] = 94
    data = {**config, "backends.json": json.dumps(parsed)}
    deploy = deepcopy(get("deploy", ORIGINAL)["spec"])
    deploy.update(replicas=1, selector={"matchLabels": POD_SELECTOR})
    template = deploy["template"]
    labels = template["metadata"]["labels"]
    labels.update(POD_SELECTOR)
    labels["app.kubernetes.io/instance"] = VERSION
    template["metadata"] = {"labels": labels}
    refs = [v for v in template["spec"]["volumes"] if v.get("configMap",{}).get("name") == BACKENDS]
    assert len(refs) == 1
    refs[0]["configMap"]["name"] = EVAL_BACKENDS
    env = {i["name"]:i for i in template["spec"]["containers"][0]["env"]}
    env["AR_SANDBOX_RECONCILE_INTERVAL_S"]["value"] = "0"
    policy = deepcopy(get("networkpolicy", ORIGINAL)["spec"])
    policy["podSelector"] = {"matchLabels": POD_SELECTOR}
    for rule in policy["ingress"]:
        for peer in rule["from"]:
            sel = peer["podSelector"]["matchLabels"]
            assert sel["app.kubernetes.io/component"] in ("task-service", "worker")
            del sel["app.kubernetes.io/instance"]
            sel[LABEL] = VERSION
    service = {"selector": POD_SELECTOR, "ports": get("service", ORIGINAL)["spec"]["ports"], "type": "ClusterIP"}
    apply_owned(body("v1", "ConfigMap", EVAL_BACKENDS, data=data))
    apply_owned(body("networking.k8s.io/v1", "NetworkPolicy", EVAL, spec=policy))
    apply_owned(body("v1", "Service", EVAL, spec=service))
    apply_owned(body("apps/v1", "Deployment", EVAL, spec=deploy))
    print(kubectl("rollout", "status", "deployment/"+EVAL, "--timeout=180s").strip())
    for name in (BACKENDS, EVAL_BACKENDS):
        parsed = json.loads(get("cm", name)["data"]["backends.json"])
        print(name, "max_images", parsed["environments"]["limits"]["max_images"])
    print("candidate service", get("service", EVAL)["spec"]["clusterIP"])
if __name__ == "__main__":
    main()
