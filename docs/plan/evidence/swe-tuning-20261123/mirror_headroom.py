"""Temporarily release verified, public SWE pull-through-cache blobs, keeping two rollback sources."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time

BASE = Path("/opt/swebench/cache/swe-tuning-20261123")
RAM = Path("/run/swe50-1123-mirror-buffer")
STATE = BASE / "mirror-buffer-state.json"
GIB = 2**30


def path_for_pvc(name):
    volume = subprocess.check_output(
        ["kubectl", "-n", "agent-runtime", "get", "pvc", name, "-o", "jsonpath={.spec.volumeName}"],
        text=True,
    )
    path = subprocess.check_output(
        ["kubectl", "get", "pv", volume, "-o", "jsonpath={.spec.local.path}"], text=True
    )
    return Path(path) / "docker/registry/v2/blobs/sha256"


def save(state):
    with tempfile.NamedTemporaryFile("w", dir=BASE, delete=False) as out:
        json.dump(state, out, sort_keys=True, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(out.name, STATE)


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def active_or_next():
    ids = set()
    for name in ("init50-gpt62-tuned-20261123", "mini50-tuned-c-gpt62-20261123"):
        root = Path("/opt/swebench/runs") / name
        cohort = json.loads((root / "run.json").read_bytes())["instance_ids"]
        records = {
            f.stem: json.loads(f.read_bytes()).get("state") for f in (root / "records").glob("*.json")
        }
        ids.update(i for i, status in records.items() if status != "finished")
        ids.update([i for i in cohort if i not in records][:4])
    return ids


def create_buffer():
    RAM.mkdir(mode=0o700, exist_ok=True)
    if not os.path.ismount(RAM):
        if any(RAM.iterdir()):
            raise RuntimeError("buffer path already contains data")
        subprocess.run(
            [
                "mount",
                "-t",
                "tmpfs",
                "-o",
                "size=13G,mode=700,nodev,nosuid,noexec",
                "swe50-1123-mirror-buffer",
                str(RAM),
            ],
            check=True,
        )
    mount_source = subprocess.check_output(
        ["findmnt", "--raw", "--noheadings", "--target", str(RAM), "-o", "SOURCE"], text=True
    ).strip()
    if mount_source != "swe50-1123-mirror-buffer":
        raise RuntimeError("buffer mount has another owner")


def hold(max_bytes, allowed, state, mirror, private):
    assert (
        json.loads((BASE / "mirror-probe-receipt.json").read_bytes())["proxy_cache_miss"]
        == "public_proxy_refetched"
    )
    create_buffer()
    choices = json.loads((BASE / "mirror-candidates.json").read_bytes())
    busy = active_or_next()
    in_ram = sum(r["size"] for r in state.values() if r["status"] == "held")
    done_only = allowed == "completed"
    ordered = sorted(choices, key=lambda h: (not choices[h]["all_done"], -choices[h]["size"]))
    for h in ordered:
        choice = choices[h]
        size = choice["size"]
        if h in state or not choice["refs_verified"] or (done_only and not choice["all_done"]):
            continue
        if set(choice["used_by"]) & busy or size + in_ram > max_bytes:
            continue
        rel = Path(h[:2]) / h / "data"
        cache = mirror / rel
        source = private / rel
        if not (cache.is_file() and source.is_file()) or os.path.samefile(cache, source):
            continue
        st = cache.stat()
        if st.st_nlink != 1 or st.st_size != size or source.stat().st_size != size or digest(source) != h:
            continue
        backup = RAM / h
        with cache.open("rb") as inp, tempfile.NamedTemporaryFile(dir=RAM, delete=False) as mem:
            shutil.copyfileobj(inp, mem, 4 * 1024**2)
            tmp = Path(mem.name)
        if digest(tmp) != h:
            tmp.unlink()
            raise RuntimeError("cache digest mismatch")
        os.replace(tmp, backup)
        # Persist provenance before dropping this secondary copy. The independent
        # registry CAS and immutable public upstream remain available without RAM.
        row = {
            "size": size,
            "public_cases": choice["used_by"],
            "all_graded_at_inventory": choice["all_done"],
            "mode": stat.S_IMODE(st.st_mode),
            "uid": st.st_uid,
            "gid": st.st_gid,
            "mtime_ns": st.st_mtime_ns,
            "held_at": int(time.time()),
            "status": "held",
        }
        state[h] = row
        save(state)
        if cache.stat().st_ino != st.st_ino or cache.stat().st_size != size:
            raise RuntimeError("cache changed")
        cache.unlink()
        in_ram += size
    print("held_bytes", in_ram, "root_free_bytes", shutil.disk_usage("/").free)


def restore(state, mirror, private):
    needed = sum(
        v["size"]
        for h, v in state.items()
        if v["status"] == "held" and not (mirror / h[:2] / h / "data").exists()
    )
    if shutil.disk_usage("/").free - needed < 50 * GIB:
        raise RuntimeError("wait until restoring these cache bytes leaves 50 GiB free")
    for h, row in state.items():
        if row["status"] != "held":
            continue
        dest = mirror / h[:2] / h / "data"
        original = private / h[:2] / h / "data"
        backup = RAM / h
        if not dest.is_file():
            src = backup if backup.is_file() else original
            if digest(src) != h:
                raise RuntimeError("restore source digest mismatch")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with src.open("rb") as f, tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as temp:
                shutil.copyfileobj(f, temp, 4 * 1024**2)
                temp.flush()
                os.fsync(temp.fileno())
                t = Path(temp.name)
            os.chown(t, row["uid"], row["gid"])
            os.chmod(t, row["mode"])
            os.utime(t, ns=(row["mtime_ns"], row["mtime_ns"]))
            if digest(t) != h:
                raise RuntimeError("restored bytes differ")
            # If the proxy populated the cache concurrently, retain its copy.
            try:
                os.link(t, dest)
            except FileExistsError:
                pass
            finally:
                t.unlink()
        if digest(dest) != h:
            raise RuntimeError("restored cache digest mismatch")
        row["status"] = "restored"
        row["restored_at"] = int(time.time())
        save(state)
        if backup.exists():
            backup.unlink()
    if RAM.is_dir() and os.path.ismount(RAM) and not any(RAM.iterdir()):
        subprocess.run(["umount", str(RAM)], check=True)
        RAM.rmdir()
    print("restored", len(state), "root_free_bytes", shutil.disk_usage("/").free)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["hold", "restore"])
    p.add_argument("--gib", type=float, default=3)
    p.add_argument("--scope", choices=("completed", "off_queue"), default="completed")
    args = p.parse_args()
    if not 0 < args.gib <= 12:
        raise ValueError("buffer limit must be at most 12 GiB")
    mirror = path_for_pvc("ar-agent-runtime-docker-mirror")
    private = path_for_pvc("ar-agent-runtime-env-registry")
    with (BASE / "mirror-buffer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(STATE.read_bytes()) if STATE.exists() else {}
        if args.action == "hold":
            hold(args.gib * GIB, args.scope, state, mirror, private)
        else:
            restore(state, mirror, private)


if __name__ == "__main__":
    main()
