"""Grade collected predictions ahead of the final report, with one host-wide grader."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


HERE = Path(__file__).resolve().parent
PYTHON = "/opt/swebench/venv/bin/python"
FLOOR = 55 * 2 ** 30


def read(path):
    return json.loads(path.read_bytes())


def sha(content):
    return hashlib.sha256(content).hexdigest()


def save(path, doc):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        json.dump(doc, stream, indent=2)
        stream.write("\n")
    Path(stream.name).replace(path)


def verify(out):
    predictions = {item["instance_id"]: item for item in (
        json.loads(line) for line in (out / "predictions.jsonl").read_text().splitlines())}
    for path in (out / "stream").glob("*/receipt.json"):
        receipt = read(path)
        item = predictions[path.parent.name]
        if sha(item["model_patch"].encode()) != receipt["patch_sha256"]:
            raise ValueError(f"Changed prediction after grading for {path.parent.name}")
        if item["model_name_or_path"] != receipt["model"] or receipt["run_id"] != out.name:
            raise ValueError(f"Wrong model or run for {path.parent.name}")


def image_ref(iid):
    return f"swebench/sweb.eval.x86_64.{iid.replace('__', '_1776_')}:latest"


def image_info(tag):
    proc = subprocess.run(["docker","image","inspect",tag], capture_output=True)
    if proc.returncode:
        return None
    info = json.loads(proc.stdout)[0]
    return {"image_id":info["Id"], "repo_digests":info["RepoDigests"] or []}


def grade_one(out, iid, rec, model):
    folder = out / "stream" / iid
    folder.mkdir(parents=True, exist_ok=True)
    grading = out / "grading"
    grading.mkdir(exist_ok=True)
    prediction = {"instance_id":iid, "model_name_or_path":model, "model_patch":rec["patch"]}
    path = folder / "prediction.jsonl"
    if path.exists() and read(path) != prediction:
        raise ValueError(f"Refusing to replace prediction for {iid}")
    if not path.exists():
        path.write_text(json.dumps(prediction) + "\n")
    tag = image_ref(iid)
    index = read(HERE / "grader-layer-index.json")[iid]
    if tag != index["image"]:
        raise ValueError(f"Unexpected Docker tag for {iid}")
    repo = tag.rsplit(":", 1)[0]
    pins = {repo + "@" + index[k] for k in ("digest", "amd64_digest")}
    cached = image_info(tag)
    daemon = os.getenv("DOCKER_HOST","unix:///var/run/docker.sock")
    if daemon != "unix:///var/run/docker.sock":
        raise ValueError("Formal scoring requires the original evaluator's Docker daemon")
    prior_file = folder / "original-image.json"
    ref = repo + "@" + index["digest"]
    if not prior_file.exists():
        # image.log from an earlier version is only opened when that attempt acquired
        # a previously absent digest and tag itself. Preserve ownership across retries.
        if (folder / "image.log").exists():
            save(prior_file, {"tag":None, "digest":None})
        else:
            save(prior_file, {"tag":cached, "digest":image_info(ref)})
    prior = read(prior_file)
    if cached is None:
        if shutil.disk_usage("/").free < FLOOR:
            return False
        ref = repo + "@" + index["digest"]
        if image_info(ref) is None:
            with (folder / "image.log").open("a") as log:
                subprocess.run(["docker", "pull", "--platform", "linux/amd64", ref], stdout=log, stderr=log, check=True)
        subprocess.run(["docker","tag",ref,tag], check=True)
    pinned = image_info(tag)
    if not pinned or pins.isdisjoint(pinned["repo_digests"]):
        raise ValueError(f"Local grader image differs from frozen digest: {iid}")
    cmd = [PYTHON,"-m","swebench.harness.run_evaluation","--dataset_name",str(out/"instances.jsonl"),
           "--split","test","--predictions_path",str(path),"--run_id",out.name,
           "--max_workers","1","--report_dir",str(folder),"--instance_ids",iid]
    with (folder / "grader.log").open("a") as log:
        official_env = dict(os.environ)
        official_env.pop("PYTHONPATH", None)
        subprocess.run(cmd, cwd=grading, env=official_env, stdout=log, stderr=subprocess.STDOUT, check=True)
    result = read(folder / f"{model}.{out.name}.json")
    installed = image_info(tag)
    in_use = subprocess.check_output(["docker","ps","-aq","--filter","ancestor="+tag]).strip() if installed else None
    if prior["tag"] is None and installed and not in_use and any(
            name.startswith(tag.rsplit(":",1)[0]+"@sha256:") for name in installed["repo_digests"]):
        subprocess.run(["docker","image","rm","--no-prune",tag],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        ref = repo + "@" + index["digest"]
        if prior["digest"] is None and image_info(ref):
            subprocess.run(["docker","image","rm","--no-prune",ref],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if iid not in result["completed_ids"] and iid not in result.get("empty_patch_ids", []):
        print(f"{iid}: grader did not return an official verdict",flush=True)
        return False
    save(folder / "receipt.json", {"run_id":out.name,"instance_id":iid,"model":model,
         "patch_sha256":sha(rec["patch"].encode()), "official_report_sha256":sha((folder/f"{model}.{out.name}.json").read_bytes()),
         "at":datetime.now(timezone.utc).isoformat(),"resolved":iid in result["resolved_ids"],
         "empty_patch":iid in result.get("empty_patch_ids",[]),"docker_host":daemon,"prior_image":prior["tag"],"used_image":installed})
    print(iid, "resolved" if iid in result["resolved_ids"] else "empty" if iid in result.get("empty_patch_ids",[]) else "unresolved",flush=True)
    return True


def stream(out, drain=False):
    model = read(out/"run.json")["settings"]["model_name"]
    ids = read(out/"run.json")["instance_ids"]
    if drain:
        ids = [json.loads(line)["instance_id"] for line in (out/"predictions.jsonl").read_text().splitlines()]
    retries = {}
    while drain or not (out/".final-grading").exists():
        if drain and all((out/"stream"/iid/"receipt.json").exists() for iid in ids):
            return
        did_work = False
        for iid in ids:
            recpath = out / "records" / (iid+".json")
            if not recpath.exists() or (out/"stream"/iid/"receipt.json").exists() or time.time()<retries.get(iid,0):
                continue
            rec = read(recpath)
            if rec.get("state") != "finished" or not isinstance(rec.get("patch"),str):
                continue
            with (HERE/"grader-host.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if not drain and (out/".final-grading").exists():
                    return
                if (out/"stream"/iid/"receipt.json").exists():
                    continue
                try:
                    did_work = grade_one(out,iid,rec,model)
                except (OSError,subprocess.CalledProcessError,KeyError) as exc:
                    print(iid, type(exc).__name__, str(exc)[:220],flush=True)
                if not did_work:
                    retries[iid] = time.time()+90
            time.sleep(1)
            if did_work:
                break
        if not did_work:
            time.sleep(15)


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("out", type=Path)
    p.add_argument("--verify", action="store_true")
    p.add_argument("--drain", action="store_true")
    args=p.parse_args()
    if args.verify:
        verify(args.out)
    else:
        with (args.out/".stream.lock").open("a") as single:
            fcntl.flock(single, fcntl.LOCK_EX if args.drain else fcntl.LOCK_EX | fcntl.LOCK_NB)
            stream(args.out, drain=args.drain)
