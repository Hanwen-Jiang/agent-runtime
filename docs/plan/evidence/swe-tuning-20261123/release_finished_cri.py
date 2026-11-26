"""Release a finished model task's idle CRI cache after checking registry backing and live pod references."""

import fcntl, hashlib, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

root = Path("/opt/swebench/cache/swe-tuning-20261123")
runs = Path("/opt/swebench/runs")
cohorts = ("init50-gpt62-tuned-20261123", "mini50-tuned-c-gpt62-20261123")


def j(cmd):
    return json.loads(subprocess.check_output(cmd))


def get(p):
    return json.loads(p.read_bytes())


def complete_registry_image(storage, digest):
    def blob(d):
        return storage / "blobs/sha256" / d[7:9] / d[7:] / "data"

    pending = [digest]
    checked = set()
    while pending:
        current = pending.pop()
        if current in checked:
            continue
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", current) or not blob(current).is_file():
            return False
        body = blob(current).read_bytes()
        if hashlib.sha256(body).hexdigest() != current[7:]:
            return False
        checked.add(current)
        item = json.loads(body)
        if "manifests" in item:
            pending.extend(child["digest"] for child in item["manifests"])
        elif "layers" in item and "config" in item:
            for child in [item["config"], *item["layers"]]:
                path = blob(child["digest"])
                if not path.is_file() or path.stat().st_size != child["size"]:
                    return False
        else:
            return False
    return True


def release():
    pv = subprocess.check_output(
        [
            "kubectl",
            "-n",
            "agent-runtime",
            "get",
            "pvc",
            "ar-agent-runtime-env-registry",
            "-o",
            "jsonpath={.spec.volumeName}",
        ],
        text=True,
    )
    storage = (
        Path(j(["kubectl", "get", "pv", pv, "-o", "json"])["spec"]["local"]["path"]) / "docker/registry/v2"
    )
    prefix = "127.0.0.1:30500/envs/swebench/env@"
    ready = {}
    busy = set()
    for name in cohorts:
        d = runs / name
        for prep in (d / "prewarm").glob("*.json"):
            record = d / "records" / prep.name
            if not record.is_file():
                continue
            raw = get(record)
            pre = get(prep)
            ref = pre.get("registry_image") or ""
            if not ref.startswith(prefix):
                continue
            if raw.get("state") == "finished" and raw.get("task_finished") is True:
                ready[ref] = name + "/" + prep.stem
            else:
                busy.add(ref)
    active_pods = j(["kubectl", "get", "pods", "-A", "-o", "json"])["items"]
    active_names = {
        c["image"]
        for pod in active_pods
        if pod.get("status", {}).get("phase") not in ("Failed", "Succeeded")
        for c in pod["spec"].get("containers", [])
    }
    containers = j(["k3s", "crictl", "ps", "-a", "-o", "json"])["containers"]
    for im in j(["k3s", "crictl", "images", "-o", "json"])["images"]:
        refs = set(im.get("repoDigests") or [])
        tags = set(im.get("repoTags") or [])
        candidates = refs & ready.keys()
        if not candidates or refs & busy or (refs | tags) & active_names:
            continue
        if any(
            c.get("imageRef") in refs | {im["id"]} or c.get("image", {}).get("image") in refs | tags
            for c in containers
        ):
            continue
        if not all(x.startswith(prefix.partition("@")[0]) for x in refs | tags):
            continue
        ref = next(iter(candidates))
        dg = ref.partition("@")[2]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", dg):
            continue
        suffix = dg[7:]
        link = storage / "repositories/envs/swebench/env/_manifests/revisions/sha256" / suffix / "link"
        if not link.is_file() or link.read_text() != dg:
            continue

        if not complete_registry_image(storage, dg):
            continue
        before = shutil.disk_usage("/").free
        removal = subprocess.run(["k3s", "crictl", "rmi", ref], capture_output=True)
        if removal.returncode:
            # Kubelet image GC or a renewed lease can win this race.
            # Keep the image and recheck against fresh state on the next sweep.
            continue
        event = {
            "at": int(time.time()),
            "digest": dg,
            "case": ready[ref],
            "free_before": before,
            "free_after": shutil.disk_usage("/").free,
        }
        with (root / "recoverable-swe-docker-cache/released-cri.jsonl").open("a") as f:
            print(json.dumps(event), file=f, flush=True)
            os.fsync(f.fileno())
        print("released", ready[ref], dg)


if __name__ == "__main__":
    with (root / "grader-host.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        release()
