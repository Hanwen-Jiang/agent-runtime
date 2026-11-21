"""Populate only missing official Linux/amd64 grader tags, by verified repository digest."""
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

inventory, destination = map(Path, sys.argv[1:])
destination.mkdir(parents=True, exist_ok=True)
rows = json.loads(inventory.read_text())["rows"]
reserve = 45 * 2**30
journal = destination / "prefetch.jsonl"


def inspect(image):
    p = subprocess.run(["docker", "image", "inspect", image], text=True, capture_output=True)
    if p.returncode:
        return None
    doc = json.loads(p.stdout)[0]
    return {"local_id": doc["Id"], "repo_digests": doc.get("RepoDigests", []), "size": doc.get("Size")}


def record(**data):
    with journal.open("a") as out:
        print(json.dumps({"at":datetime.datetime.now(datetime.timezone.utc).isoformat(), **data}), file=out, flush=True)
        os.fsync(out.fileno())


with tempfile.TemporaryDirectory(prefix="mini50-public-docker-") as public_config:
    env = dict(os.environ, DOCKER_CONFIG=public_config)
    for r in rows:
        image = r["image"]
        assert re.fullmatch(r"swebench/sweb\.eval\.x86_64\.[a-z0-9_-]+:latest", image)
        assert re.fullmatch(r"sha256:[0-9a-f]{64}",r["registry_manifest_digest"])
        ref = image.rsplit(":",1)[0] + "@" + r["registry_manifest_digest"]
        present = inspect(image)
        if present is not None:
            record(image=image,status="already_cached",digest_matches=ref in present["repo_digests"],**present)
            continue
        free = shutil.disk_usage("/var/lib/docker").free
        if free < reserve:
            record(image=image,status="stopped_space_floor",free_bytes=free);sys.exit(3)
        try:
            p = subprocess.run(["docker","pull","--platform","linux/amd64",ref],env=env,
                               text=True,capture_output=True,timeout=300)
        except subprocess.TimeoutExpired:
            record(image=image,status="pull_timeout");sys.exit(2)
        if p.returncode:
            msg = p.stderr.lower()
            reason = next((x for x in ("toomanyrequests","429","no space","timeout","denied","not found","tls") if x in msg),"registry_error")
            record(image=image,status="pull_failed",reason=reason);sys.exit(2)
        if inspect(image) is None:
            subprocess.run(["docker","tag",ref,image],check=True)
        installed=inspect(image);pinned=inspect(ref)
        if installed is None or pinned is None or installed["local_id"]!=pinned["local_id"]:
            record(image=image,status="tag_changed_concurrently");sys.exit(2)
        record(image=image,status="fetched_by_digest",**installed,free_bytes=shutil.disk_usage("/var/lib/docker").free)
record(status="finished")
