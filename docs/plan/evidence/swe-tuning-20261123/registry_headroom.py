"""Share verified read-only bytes for newly published SWE cache blobs while retaining every OCI reference."""

import argparse, fcntl, hashlib, json, os, shutil, stat, subprocess, tempfile, time
from pathlib import Path

B = Path("/opt/swebench/cache/swe-tuning-20261123")
DC = Path("/var/lib/containerd/io.containerd.content.v1.content/blobs/sha256")
STATE = B / "registry-headroom-state.json"
GIB = 2**30


def read(p):
    return json.loads(p.read_bytes())


def sha(p):
    with p.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def save(d):
    with tempfile.NamedTemporaryFile("w", dir=B, delete=False) as f:
        json.dump(d, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(f.name, STATE)


def storage():
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
    return (
        Path(
            subprocess.check_output(
                ["kubectl", "get", "pv", pv, "-o", "jsonpath={.spec.local.path}"], text=True
            )
        )
        / "docker/registry/v2"
    )


def blob(store, h):
    return store / "blobs/sha256" / h[:2] / h / "data"


def completed():
    runs = Path("/opt/swebench/runs")
    g = read(B / "grader-layer-index.json")
    info = []
    for n in ("init50-gpt62-tuned-20261123", "mini50-tuned-c-gpt62-20261123"):
        p = runs / n
        plan = set(read(p / "run.json")["instance_ids"])
        done = {f.parent.name for f in (p / "stream").glob("*/receipt.json")}
        info.append((plan, done))
    return {i for i in g if all(i not in plan or i in finished for plan, finished in info)}


def choices(store, state, off_queue=False, queue_ahead=4):
    data = read(B / "grader-layer-index.json")
    refs = {}
    sizes = {}
    for i, row in data.items():
        for layer in row["layers"]:
            h = layer["digest"][7:]
            refs.setdefault(h, set()).add(i)
            sizes[h] = layer["bytes"]
    names = {}
    for p in (store / "repositories").rglob("link"):
        if p.parent.parent.parent.name == "_layers" and p.parent.name in sizes:
            names.setdefault(p.parent.name, set()).add(
                str(p.parent.parent.parent.parent.relative_to(store / "repositories"))
            )
    done = completed()
    near = set()
    for name in ("init50-gpt62-tuned-20261123", "mini50-tuned-c-gpt62-20261123"):
        run = Path("/opt/swebench/runs") / name
        order = read(run / "run.json")["instance_ids"]
        stages = {path.stem: read(path).get("state") for path in (run / "records").glob("*.json")}
        near.update(i for i, stage in stages.items() if stage != "finished")
        near.update([i for i in order if i not in stages][:queue_ahead])
    cut = (B / "init-50/preflight-inventory.json").stat().st_mtime
    for h in sorted(sizes, key=lambda h: -sizes[h]):
        if (
            h in state
            or sizes[h] < 1024**2
            or (not refs[h] <= done and (not off_queue or refs[h] & near))
            or names.get(h) != {"envs/swebench/env"}
        ):
            continue
        dest = blob(store, h)
        src = DC / h
        if not (dest.is_file() and src.is_file()):
            continue
        a, z = dest.stat(), src.stat()
        if (
            a.st_ctime < cut
            or a.st_uid != 10001
            or a.st_nlink != 1
            or z.st_uid != 0
            or z.st_mode & 0o222
            or os.path.samefile(dest, src)
            or z.st_dev != a.st_dev
        ):
            continue
        if a.st_size != sizes[h] or z.st_size != sizes[h] or os.listxattr(dest) or os.listxattr(src):
            continue
        yield (
            h,
            {
                "size": a.st_size,
                "uid": a.st_uid,
                "gid": a.st_gid,
                "mode": stat.S_IMODE(a.st_mode),
                "atime_ns": a.st_atime_ns,
                "mtime_ns": a.st_mtime_ns,
                "cases": sorted(refs[h]),
                "new_cache_ctime": a.st_ctime,
            },
        )


def verify_get(paths):
    # Reuse the evaluator worker's existing scoped registry identity. Never export credentials.
    source = (B / "init-50/publish_cached_oci.py").read_text().split("def main():")[0]
    script = (
        source
        + """
items=json.load(sys.stdin)
client=Registry()
out={}
for h in items:
    c=HTTPConnection(HOST,timeout=15)
    try:
        c.request('GET',REPO+'/blobs/sha256:'+h,headers={**client.headers,'Range':'bytes=0-65535'})
        r=c.getresponse()
        if r.status not in (200,206): raise RuntimeError('registry returned HTTP '+str(r.status))
        out[h]=sha256(r.read(65536)).hexdigest()
    finally:c.close()
print(json.dumps(out))
"""
    )
    run = subprocess.run(
        [
            "kubectl",
            "-n",
            "agent-runtime",
            "exec",
            "-i",
            "deploy/swe50-1123-c-program",
            "--",
            "python",
            "-c",
            script,
        ],
        input=json.dumps(list(paths)).encode(),
        capture_output=True,
        timeout=120,
    )
    if run.returncode:
        raise RuntimeError("private registry GET failed")
    response = json.loads(run.stdout)
    for h, p in paths.items():
        with p.open("rb") as f:
            expect = hashlib.sha256(f.read(65536)).hexdigest()
        if response.get(h) != expect:
            raise RuntimeError("private registry returned different bytes")


def share(store, state, max_gib, probe, off_queue=False, queue_ahead=4, stop_free_gib=None):
    current = sum(v["size"] for v in state.values() if v["status"] == "shared")
    queue = list(choices(store, state, off_queue=off_queue, queue_ahead=queue_ahead))
    if probe:
        queue = sorted(queue, key=lambda x: x[1]["size"])[:1]
    todo = []
    for h, row in queue:
        if current + row["size"] > max_gib * GIB:
            continue
        todo.append((h, row))
        current += row["size"]
    for start in range(0, len(todo), 4):
        if stop_free_gib is not None and shutil.disk_usage("/").free >= stop_free_gib * GIB:
            break
        batch = todo[start : start + 4]
        for h, row in batch:
            dest = blob(store, h)
            src = DC / h
            if sha(dest) != h or sha(src) != h:
                raise ValueError("OCI source digest mismatch")
        verify_get({h: blob(store, h) for h, _ in batch})
        changed = []
        try:
            for h, row in batch:
                dest = blob(store, h)
                src = DC / h
                old = dest.with_name(".swe50-original")
                new = dest.with_name(".swe50-readonly")
                if (
                    old.exists()
                    or new.exists()
                    or dest.stat().st_ctime != row["new_cache_ctime"]
                    or os.path.samefile(dest, src)
                ):
                    raise RuntimeError("source changed concurrently")
                state[h] = {**row, "status": "preparing", "at": int(time.time())}
                save(state)
                os.link(dest, old)
                os.link(src, new)
                os.replace(new, dest)
                changed.append((h, old, dest))
            verify_get({h: dest for h, _, dest in changed})
        except BaseException:
            for h, old, dest in reversed(changed):
                os.replace(old, dest)
                del state[h]
            save(state)
            raise
        else:
            for h, old, dest in changed:
                assert dest.stat().st_mode & 0o222 == 0 and os.path.samefile(DC / h, dest)
                state[h].update(status="shared", registry_get_verified=True)
                save(state)
                old.unlink()
    print(
        "shared bytes",
        sum(v["size"] for v in state.values() if v["status"] == "shared"),
        "root free",
        shutil.disk_usage("/").free,
        flush=True,
    )


def restore(store, state, within_reserve=False):
    needed = sum(v["size"] for v in state.values() if v["status"] == "shared")
    if not within_reserve and shutil.disk_usage("/").free - needed < 50 * GIB:
        raise RuntimeError("insufficient disk for full independent inode restoration")
    for h, row in sorted(state.items(), key=lambda item: -item[1]["size"]):
        if row["status"] != "shared":
            continue
        if within_reserve and shutil.disk_usage("/").free - row["size"] < 50 * GIB:
            continue
        dest = blob(store, h)
        if sha(dest) != h:
            raise ValueError("registry blob digest changed")
        with dest.open("rb") as f, tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
            shutil.copyfileobj(f, tmp, 4 * 1024**2)
            tmp.flush()
            os.fsync(tmp.fileno())
            new = Path(tmp.name)
        if sha(new) != h:
            raise ValueError("rollback digest mismatch")
        os.chown(new, row["uid"], row["gid"])
        os.chmod(new, row["mode"])
        os.utime(new, ns=(row["atime_ns"], row["mtime_ns"]))
        os.replace(new, dest)
        verify_get({h: dest})
        row.update(status="restored", restored_at=int(time.time()))
        save(state)
    print(
        "independent inodes",
        sum(row["status"] == "restored" for row in state.values()),
        "shared read-only bytes",
        sum(row["size"] for row in state.values() if row["status"] == "shared"),
        "free bytes",
        shutil.disk_usage("/").free,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("share", "restore"))
    parser.add_argument("--gib", type=float, default=8)
    parser.add_argument("--stop-free-gib", type=float)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--off-queue", action="store_true")
    parser.add_argument("--within-reserve", action="store_true")
    parser.add_argument("--queue-ahead", type=int, choices=(0, 4), default=4)
    a = parser.parse_args()
    assert 0 < a.gib <= 16
    with (B / "registry-headroom.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        s = read(STATE) if STATE.exists() else {}
        store = storage()
        if a.action == "share":
            share(
                store,
                s,
                a.gib,
                a.probe,
                off_queue=a.off_queue,
                queue_ahead=a.queue_ahead,
                stop_free_gib=a.stop_free_gib,
            )
        else:
            restore(store, s, within_reserve=a.within_reserve)
