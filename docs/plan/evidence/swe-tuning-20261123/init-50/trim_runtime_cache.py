"""Release this run's idle node cache under pressure after verifying the registry copy."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

IMAGE = re.compile(r"127\.0\.0\.1:30500/envs/swebench/env@(sha256:[0-9a-f]{64})")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

def command(*argv):
    return subprocess.check_output(argv,text=True)

def data(path):
    return json.loads(path.read_bytes())

def registry_storage():
    claim = json.loads(command("kubectl","-n","agent-runtime","get","pvc","ar-agent-runtime-env-registry","-o","json"))
    volume = json.loads(command("kubectl","get","pv",claim["spec"]["volumeName"],"-o","json"))
    return Path((volume["spec"].get("local") or volume["spec"]["hostPath"])["path"])/"docker/registry/v2"

def recoverable(store, digest):
    if not DIGEST.fullmatch(digest):
        return False
    h=digest[7:]
    repo=store/"repositories/envs/swebench/env/_manifests/revisions/sha256"/h/"link"
    if not repo.is_file() or repo.read_text()!=digest:
        return False
    def blob(s):
        return store/"blobs/sha256"/s[7:9]/s[7:]/"data"
    def reachable(ref):
        p=blob(ref)
        if not p.is_file():
            return False
        payload=p.read_bytes()
        if hashlib.sha256(payload).hexdigest()!=ref[7:]:
            return False
        manifest=json.loads(payload)
        if "manifests" in manifest:
            return all(DIGEST.fullmatch(child["digest"]) and reachable(child["digest"]) for child in manifest["manifests"])
        return all(DIGEST.fullmatch(entry["digest"]) and blob(entry["digest"]).is_file()
                   and blob(entry["digest"]).stat().st_size==entry["size"] for entry in [manifest["config"],*manifest["layers"]])
    return reachable(digest)


def main():
    p=argparse.ArgumentParser()
    p.add_argument("run",type=Path)
    p.add_argument("inventory",type=Path)
    args=p.parse_args()
    if (args.inventory.parent/"external-cache-manager.active").exists():
        return
    new={r["id"] for r in data(args.inventory) if r["db_state"]=="deleted" and not r["original_revision_exists"]}
    store=registry_storage()
    for f in sorted((args.run/"records").glob("*.json")):
        if shutil.disk_usage("/").free>=55*2**30:
            return
        rec=data(f)
        if f.stem not in new or rec.get("state")!="finished" or not rec.get("task_finished") or not rec.get("task_id"):
            continue
        proof=data(args.run/"prewarm"/f.name)
        ref=proof.get("registry_image","")
        match=IMAGE.fullmatch(ref)
        if not proof.get("ready") or not proof.get("released") or not match or not recoverable(store,match[1]):
            continue
        result=subprocess.run(["k3s","crictl","inspecti",ref],text=True,capture_output=True)
        if result.returncode:
            continue
        status=json.loads(result.stdout)["status"]
        if ref not in status["repoDigests"] or status["id"]!=proof["cri_config_id"]:
            continue
        containers=json.loads(command("k3s","crictl","ps","-a","-o","json"))["containers"]
        references={ref,match[1],status["id"]}
        if any(c.get("imageRef") in references or c.get("image",{}).get("image") in references for c in containers):
            continue
        before=shutil.disk_usage("/").free
        subprocess.run(["k3s","crictl","rmi",ref],check=True,capture_output=True,text=True)
        print(json.dumps({"at":time.time(),"id":f.stem,"registry_image":ref,"node_before":before,
                          "node_after":shutil.disk_usage("/").free}),flush=True)
if __name__=="__main__":
    main()
