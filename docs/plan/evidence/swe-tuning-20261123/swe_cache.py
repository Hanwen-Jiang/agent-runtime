"""Inventory Docker's historical SWE wrappers and recover exact tags without a second full local archive."""

import argparse, hashlib, io, json, os, re, shutil, subprocess, sys, tarfile, time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode

base = Path("/opt/swebench/cache/swe-tuning-20261123")
dest = base / "recoverable-swe-docker-cache"
dest.mkdir(exist_ok=True)
cas = Path("/var/lib/containerd/io.containerd.content.v1.content/blobs/sha256")
repo = "127.0.0.1:30500/envs/swebench/env"
index = json.loads((base / "grader-layer-index.json").read_bytes())
sources = {}
for item in index.values():
    for layer in item["layers"]:
        sources[layer["digest"]] = item["image"].split(":")[0]


def dpath(root, dg):
    assert re.fullmatch("sha256:[0-9a-f]{64}", dg)
    return root / dg[7:]


def do(*cmd):
    return subprocess.check_output(cmd, text=True).strip()


def read(dg):
    p = dpath(cas, dg)
    if not p.exists():
        p = dpath(dest / "blobs", dg)
    b = p.read_bytes()
    assert hashlib.sha256(b).hexdigest() == dg[7:]
    return json.loads(b)


def inventory():
    (dest / "blobs").mkdir(exist_ok=True)
    tags = {}
    for s in do(
        "docker", "image", "ls", "--digests", "--format", "{{.Tag}} {{.Digest}}", repo
    ).splitlines():
        tag, dg = s.split()
        if not re.fullmatch("[a-f0-9]{32}", tag):
            continue
        queue = [dg]
        descs = {}
        while queue:
            d = queue.pop()
            p = dpath(cas, d)
            if d in descs:
                continue
            j = read(d)
            descs[d] = {"mediaType": j["mediaType"], "size": p.stat().st_size, "digest": d}
            if "manifests" in j:
                queue.extend(it["digest"] for it in j["manifests"])
            else:
                for it in [j["config"], *j.get("layers", [])]:
                    descs[it["digest"]] = {k: it[k] for k in ["mediaType", "size", "digest"]}
        for d, desc in descs.items():
            if d in sources:
                continue
            src = dpath(cas, d)
            p = dpath(dest / "blobs", d)
            if not p.exists():
                # Pin only bytes without a verified published official grader source,
                # maintaining the compressed OCI bytes even after Docker forgets its copy.
                os.link(src, p)
            assert p.stat().st_size == desc["size"]
        tags[tag] = {"root": dg, "descriptors": descs}
    prior = dest / "inventory.json"
    saved = json.loads(prior.read_bytes()) if prior.exists() else {}
    saved.update(tags)
    tmp = dest / "inventory.next"
    tmp.write_text(json.dumps(saved, sort_keys=True))
    tmp.replace(prior)
    for p in (dest / "blobs").iterdir():
        h = hashlib.sha256()
        with p.open("rb") as f:
            while b := f.read(4 * 1024 * 1024):
                h.update(b)
        assert h.hexdigest() == p.name
    print(
        "verified",
        len(tags),
        "tags, local backups MiB",
        round(sum(p.stat().st_size for p in (dest / "blobs").iterdir()) / 2**20, 2),
    )


def source(d):
    for root in [dest / "blobs", cas]:
        p = dpath(root, d)
        if p.is_file():
            return p.open("rb")
    name = sources[d]
    u = "https://auth.docker.io/token?" + urlencode(
        {"service": "registry.docker.io", "scope": "repository:" + name + ":pull"}
    )
    with urlopen(u, timeout=45) as res:
        t = json.load(res)["token"]
    return urlopen(
        Request(
            "https://registry-1.docker.io/v2/" + name + "/blobs/" + d,
            headers={"Authorization": "Bearer " + t},
        ),
        timeout=120,
    )


def restore(tag, alias=None):
    row = json.loads((dest / "inventory.json").read_bytes())[tag]
    descs = row["descriptors"]
    desc = descs[row["root"]].copy()
    full = repo + ":" + (alias or tag)
    desc["annotations"] = {"io.containerd.image.name": full, "org.opencontainers.image.ref.name": full}

    def member(tar, name, f, size):
        ti = tarfile.TarInfo(name)
        ti.size = size
        tar.addfile(ti, f)

    with subprocess.Popen(
        ["docker", "image", "load"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL
    ) as pr:
        with tarfile.open(mode="w|", fileobj=pr.stdin) as out:
            for name, data in [
                ("index.json", {"schemaVersion": 2, "manifests": [desc]}),
                ("oci-layout", {"imageLayoutVersion": "1.0.0"}),
            ]:
                content = json.dumps(data).encode()
                member(out, name, io.BytesIO(content), len(content))
            for dg, data in descs.items():
                with source(dg) as f:
                    member(out, "blobs/sha256/" + dg[7:], f, data["size"])
        pr.stdin.close()
        assert pr.wait() == 0
    got = json.loads(do("docker", "image", "inspect", full))[0]
    assert got["Descriptor"]["digest"] == row["root"], (got["Descriptor"]["digest"], row["root"])
    print("restored", full, row["root"])


def release(tag):
    row = json.loads((dest / "inventory.json").read_bytes())[tag]
    img = repo + ":" + tag
    observed = json.loads(do("docker", "image", "inspect", img))[0]
    if observed["Descriptor"]["digest"] != row["root"]:
        raise ValueError("The current tag differs from the recoverable image")
    for dg, desc in row["descriptors"].items():
        if dg not in sources and dpath(dest / "blobs", dg).stat().st_size != desc["size"]:
            raise ValueError("An archived OCI blob changed size")
    if do("docker", "container", "ls", "-aq", "--filter", "ancestor=" + img):
        return False
    before = shutil.disk_usage("/").free
    subprocess.run(["docker", "image", "rm", img], check=True, stdout=subprocess.DEVNULL)
    with (dest / "released.jsonl").open("a") as log:
        print(
            json.dumps(
                {
                    "tag": img,
                    "digest": row["root"],
                    "at": int(time.time()),
                    "free_before": before,
                    "free_after": shutil.disk_usage("/").free,
                }
            ),
            file=log,
            flush=True,
        )
        os.fsync(log.fileno())
    print("released", img)
    return True


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("action", choices=["inventory", "restore", "release"])
    a.add_argument("tag", nargs="?")
    a.add_argument("--alias")
    v = a.parse_args()
    if v.action == "inventory":
        inventory()
    elif v.action == "restore":
        restore(v.tag, v.alias)
    else:
        release(v.tag)
