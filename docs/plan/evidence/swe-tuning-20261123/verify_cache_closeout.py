"""Check visibility and restore provenance of SWE cache material without requiring extra storage."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import mirror_headroom
import registry_headroom

BASE = Path(__file__).resolve().parent
ROLLBACK = BASE / "recoverable-swe-docker-cache"
DC = Path("/var/lib/containerd/io.containerd.content.v1.content/blobs/sha256")
SOURCE = "127.0.0.1:30500/envs/swebench/env:"


def read(path):
    return json.loads(path.read_bytes())


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def require(value, message):
    if not value:
        raise RuntimeError(message)


def original_docker_sources(release_backup):
    inventory = read(ROLLBACK / "inventory.json")
    names = [SOURCE + tag for tag in inventory]
    images = json.loads(subprocess.check_output(["docker", "image", "inspect", *names]))
    require(len(images) == len(inventory) == 54, "historical image count mismatch")
    for (tag, original), image in zip(inventory.items(), images):
        require(
            image["Descriptor"]["digest"] == original["root"] and SOURCE + tag in image["RepoTags"],
            "original Docker reference changed",
        )
    old = ROLLBACK / "blobs"
    counts = Counter()
    if old.is_dir():
        for f in old.iterdir():
            require(f.is_file() and not f.is_symlink() and len(f.name) == 64, "unexpected backup entry")
            src = DC / f.name
            if not src.is_file() or src.stat().st_size != f.stat().st_size or digest(src) != f.name:
                counts["retained_unverified_backup_files"] += 1
                continue
            counts["matched_backup_files"] += 1
            counts["matched_backup_bytes"] += f.stat().st_size
            if release_backup:
                counts["backup_links_removed"] += 1
                f.unlink()
        if release_backup and not any(old.iterdir()):
            old.rmdir()
    return {"original_tags_and_digests_verified": len(images), **counts}


def mirror_state():
    state = read(BASE / "mirror-buffer-state.json")
    mirror = mirror_headroom.path_for_pvc("ar-agent-runtime-docker-mirror")
    verified = 0
    metadata_matches = 0
    for h, row in state.items():
        dest = mirror / h[:2] / h / "data"
        if row["status"] == "restored":
            require(dest.is_file() and digest(dest) == h, "wrong restored mirror content")
            verified += 1
            st = dest.stat()
            if (st.st_uid, st.st_gid, st.st_mode & 0o777, st.st_mtime_ns) == (
                row["uid"],
                row["gid"],
                row["mode"],
                row["mtime_ns"],
            ):
                metadata_matches += 1
    return {
        "records": len(state),
        "restored_blobs_verified": verified,
        "original_owner_mode_mtime_verified": metadata_matches,
        "buffer_mounted": os.path.ismount(mirror_headroom.RAM),
        "restored_bytes": sum(r["size"] for r in state.values() if r["status"] == "restored"),
    }


def registry_state():
    state = read(BASE / "registry-headroom-state.json")
    store = registry_headroom.storage()
    private = {}
    inode_manifest = {}
    counts = Counter()
    for h, row in state.items():
        dest = registry_headroom.blob(store, h)
        require(dest.is_file() and digest(dest) == h, "wrong authorized registry content")
        stats = dest.stat()
        link = store / "repositories/envs/swebench/env/_layers/sha256" / h / "link"
        require(link.read_text().strip() == "sha256:" + h, "registry layer reference changed")
        if row["status"] == "restored":
            require(
                stats.st_uid == row["uid"]
                and stats.st_gid == row["gid"]
                and stats.st_mode & 0o777 == row["mode"],
                "fresh private inode metadata differs",
            )
            require(
                not (DC / h).exists() or not os.path.samefile(DC / h, dest),
                "physical duplication still pending",
            )
        elif row["status"] == "shared":
            require(
                stats.st_uid == 0
                and stats.st_mode & 0o222 == 0
                and (DC / h).exists()
                and os.path.samefile(dest, DC / h),
                "unverified shared private inode",
            )
            require(stats.st_nlink >= 2, "durable CAS reference missing")
        else:
            raise ValueError("uncommitted registry transition")
        private[h] = dest
        inode_manifest[h] = {
            "status": row["status"],
            "bytes": stats.st_size,
            "device": stats.st_dev,
            "inode": stats.st_ino,
            "nlink": stats.st_nlink,
            "uid": stats.st_uid,
            "gid": stats.st_gid,
            "mode": oct(stats.st_mode & 0o777),
        }
        counts[row["status"] + "_blobs"] += 1
        counts[row["status"] + "_bytes"] += row["size"]
    for start in range(0, len(private), 16):
        registry_headroom.verify_get(dict(list(private.items())[start : start + 16]))
    return {
        "records": len(state),
        "authenticated_GET_verified": len(private),
        "registry_layer_links_verified": len(inode_manifest),
        "inode_manifest": inode_manifest,
        **counts,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-backup", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "observed_utc": datetime.now(timezone.utc).isoformat(),
                "historical_docker": original_docker_sources(args.release_backup),
                "mirror": mirror_state(),
                "private_registry": registry_state(),
                "free_bytes": shutil.disk_usage("/").free,
            },
            indent=2,
        )
    )
