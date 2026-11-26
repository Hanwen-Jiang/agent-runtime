"""Deploy one candidate from the live, frozen B arm, in its own Temporal namespace."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

BASE = "mini50-1121-b"
PREFIX = "swe50-1123-c"
NS = "agent-runtime"
TEMPORAL = "ar-swe50-20261123-c"
OWNER = {"agentruntime.io/tuning": "swe50-1123"}
PACKAGE = "/app/services/py-worker/src/agentruntime_worker/"
SOURCE = "executor/tool_loop.py"
POOLS = ("api", "task", "workspace", "decision", "model", "program")


def kub(*args, body=None):
    return subprocess.check_output(["kubectl", "-n", NS, *args], input=body)


def resource(kind, name):
    return json.loads(kub("get", kind, name, "-o", "json"))


def candidate_meta(name):
    return {"name": name, "namespace": NS, "labels": OWNER}


def changed_labels(labels):
    return {**labels, **OWNER, "agentruntime.io/arm": "c"}


def resources(frozen, code):
    digest = hashlib.sha256(code.encode()).hexdigest()
    assert frozen["candidate_files"][SOURCE] == digest
    cm_name = PREFIX + "-prompt-" + digest[:10]
    yield {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": candidate_meta(cm_name),
        "immutable": True,
        "data": {"tool_loop.py": code},
    }
    for kind in ("Service", "NetworkPolicy", "Deployment"):
        for pool in POOLS if kind == "Deployment" else ("api",):
            name = f"{PREFIX}-{pool}"
            original = resource(kind, f"{BASE}-{pool}")
            spec = original["spec"]
            if kind == "Deployment":
                assert original["status"].get("readyReplicas", 0) == 1
                template = spec["template"]
                labels = changed_labels(template["metadata"]["labels"])
                spec["selector"]["matchLabels"] = changed_labels(spec["selector"]["matchLabels"])
                template["metadata"] = {"labels": labels}
                pod = template["spec"]
                assert len(pod["containers"]) == 1
                container = pod["containers"][0]
                for setting in container["env"]:
                    if setting["name"] == "AR_TEMPORAL_NAMESPACE":
                        assert setting["value"] == "ar-mini50-20261121-b"
                        setting["value"] = TEMPORAL
                    elif setting["name"] in (
                        "AR_TASK_SERVICE_INTERNAL_URL",
                        "AR_PUBLIC_BASE_URL",
                        "OTEL_SERVICE_VERSION",
                    ):
                        setting["value"] = setting["value"].replace(BASE, PREFIX)
                    elif setting["name"] == "AR_SANDBOX_SERVICE_ADDRESS":
                        setting["value"] = "swe50-1123-sandbox:8097"
                if pool != "api":
                    labels["agentruntime.io/pool"] = name
                    for volume in pod["volumes"]:
                        if volume["name"] == "sandbox-backends":
                            volume["configMap"]["name"] = "swe50-1123-sandbox-backends"
                    pod["volumes"].append({"name": "tuned-prompt", "configMap": {"name": cm_name}})
                    container["volumeMounts"].append(
                        {
                            "name": "tuned-prompt",
                            "mountPath": PACKAGE + SOURCE,
                            "subPath": "tool_loop.py",
                            "readOnly": True,
                        }
                    )
            elif kind == "Service":
                spec = {
                    "type": "ClusterIP",
                    "selector": changed_labels(spec["selector"]),
                    "ports": spec["ports"],
                }
            else:
                spec["podSelector"]["matchLabels"] = changed_labels(spec["podSelector"]["matchLabels"])
                for rule in spec["ingress"]:
                    for source in rule["from"]:
                        selector = source["podSelector"]
                        selector["matchLabels"] = changed_labels(selector["matchLabels"])
            yield {
                "apiVersion": original["apiVersion"],
                "kind": kind,
                "metadata": candidate_meta(name),
                "spec": spec,
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("code", type=Path)
    parser.add_argument("--freeze", type=Path, default=Path(__file__).with_name("freeze.json"))
    args = parser.parse_args()
    frozen = json.loads(args.freeze.read_bytes())
    for name, expected in frozen["parent_files"].items():
        remote = kub("exec", "deploy/" + BASE + "-model", "--", "sha256sum", PACKAGE + name)
        assert remote.split()[0].decode() == expected, name
    docs = list(resources(frozen, args.code.read_text()))
    for d in docs:
        old = kub("get", d["kind"], d["metadata"]["name"], "--ignore-not-found", "-o", "json")
        if old and not OWNER.items() <= json.loads(old)["metadata"]["labels"].items():
            raise RuntimeError("Name already in use by another deployment")
        kub("apply", "--dry-run=server", "-f", "-", body=json.dumps(d).encode())
    temporal = [
        "kubectl",
        "-n",
        "data",
        "exec",
        "deploy/temporal-admintools",
        "--",
        "temporal",
        "operator",
        "namespace",
    ]
    shared = ["--namespace", TEMPORAL, "--address", "temporal-frontend:7233"]
    if subprocess.run([*temporal, "describe", *shared], capture_output=True).returncode:
        subprocess.run([*temporal, "create", *shared, "--retention", "7d"], check=True)
    for d in docs:
        kub("apply", "-f", "-", body=json.dumps(d).encode())
    print("Candidate configuration applied", frozen["candidate_tree"])


if __name__ == "__main__":
    main()
