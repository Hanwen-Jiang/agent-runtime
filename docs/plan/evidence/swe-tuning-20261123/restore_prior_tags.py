"""Restore recorded Docker tags from their exact OCI descriptors already present in the original engine."""

import fcntl
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import time

from swe_cache import base, dest, repo


def inspect(tag):
    result = subprocess.run(["docker", "image", "inspect", tag], capture_output=True)
    if result.returncode:
        return None
    return json.loads(result.stdout)[0]["Descriptor"]["digest"]


def restore_one():
    if shutil.disk_usage("/").free < 50 * 2**30:
        return "low disk"
    inventory = json.loads((dest / "inventory.json").read_bytes())
    for line in (dest / "released.jsonl").read_text().splitlines():
        event = json.loads(line)
        tag, wanted = event["tag"], event["digest"]
        row = inventory[tag.removeprefix(repo + ":")]
        assert row["root"] == wanted
        observed = inspect(tag)
        if observed:
            if observed != wanted:
                raise ValueError(f"Existing tag now has another digest: {tag}")
            continue
        available = set(
            subprocess.check_output(["ctr", "-n", "moby", "content", "ls", "-q"], text=True).splitlines()
        )
        descriptors = row["descriptors"]
        missing = {digest: d for digest, d in descriptors.items() if digest not in available}
        if sum(d["size"] for d in missing.values()) > 128 * 1024 or any(
            not (dest / "blobs" / digest[7:]).is_file() for digest in missing
        ):
            return "additional blob fetch required"
        descriptor = {
            **descriptors[wanted],
            "annotations": {"io.containerd.image.name": tag, "org.opencontainers.image.ref.name": tag},
        }
        payloads = {
            "index.json": json.dumps({"schemaVersion": 2, "manifests": [descriptor]}).encode(),
            "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
        }
        for digest in missing:
            data = (dest / "blobs" / digest[7:]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == digest[7:]
            payloads["blobs/sha256/" + digest[7:]] = data
        # The production Docker engine's moby namespace already holds every larger OCI layer.
        # Local import updates its image metadata and avoids unpacking or writing new filesystem layers.
        with subprocess.Popen(
            ["ctr", "-n", "moby", "images", "import", "--local", "--all-platforms", "--no-unpack", "-"],
            stdin=subprocess.PIPE,
        ) as process:
            with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                for name, data in payloads.items():
                    item = tarfile.TarInfo(name)
                    item.size = len(data)
                    archive.addfile(item, io.BytesIO(data))
            process.stdin.close()
            if process.wait():
                raise RuntimeError("OCI import failed")
        assert inspect(tag) == wanted
        event = {
            "epoch": int(time.time()),
            "tag": tag,
            "restored_index": wanted,
            "bytes_from_backup": sum(d["size"] for d in missing.values()),
        }
        with (dest / "restored.jsonl").open("a") as log:
            print(json.dumps(event), file=log)
        return tag
    return "all restored"


if __name__ == "__main__":
    with (base / "grader-host.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("scorer busy")
        else:
            print(restore_one())
