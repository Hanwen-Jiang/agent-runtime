import hashlib, json, os, subprocess, sys, time
from pathlib import Path
from urllib.request import Request, urlopen

b = Path("/opt/swebench/cache/swe-tuning-20261123")
c = json.loads((b / "mirror-candidates.json").read_bytes())
d = json.loads((b / "grader-layer-index.json").read_bytes())
eligible = sorted((v["size"], k, v) for k, v in c.items() if v["all_done"])
size, h, v = eligible[0]
iid = v["used_by"][0]


def vol(pv):
    return (
        Path(
            subprocess.check_output(
                ["kubectl", "get", "pv", pv, "-o", "jsonpath={.spec.local.path}"], text=True
            )
        )
        / "docker/registry/v2/blobs/sha256"
        / h[:2]
        / h
        / "data"
    )


mirror = vol("pvc-28ba4a8a-4622-47e7-b4db-fc1a2daa80a2")
auth = vol("pvc-45304a2e-2d17-454e-b264-689fed7b485c")
for p in (mirror, auth):
    with p.open("rb") as f:
        assert hashlib.file_digest(f, "sha256").hexdigest() == h
assert not os.path.samefile(mirror, auth)
old = mirror.with_name("swe50-probe-" + str(os.getpid()))
assert not old.exists()
svc = subprocess.check_output(
    [
        "kubectl",
        "-n",
        "agent-runtime",
        "get",
        "svc",
        "ar-agent-runtime-docker-mirror",
        "-o",
        "jsonpath={.spec.clusterIP}",
    ],
    text=True,
)
ref = d[iid]["image"].split(":")[0]
url = f"http://{svc}:5000/v2/{ref}/blobs/sha256:{h}"
request = Request(url, headers={"Range": "bytes=0-65535"})
with urlopen(request, timeout=20) as response:
    before = response.read(65536)
    assert response.status in (200, 206)
os.rename(mirror, old)
try:
    with urlopen(request, timeout=60) as response:
        after = response.read(65536)
        assert response.status in (200, 206) and after == before
    source = "public_proxy_refetched"
finally:
    os.replace(old, mirror)
    with mirror.open("rb") as f:
        assert hashlib.file_digest(f, "sha256").hexdigest() == h
print(
    json.dumps(
        {
            "instance": iid,
            "original_sha256": h,
            "size": size,
            "proxy_cache_miss": source,
            "sample_bytes": len(after),
            "restored": True,
        }
    )
)
