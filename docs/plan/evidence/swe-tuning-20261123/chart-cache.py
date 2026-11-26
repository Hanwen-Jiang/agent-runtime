from pathlib import Path
import json, subprocess, collections

b = Path("/opt/swebench/cache/swe-tuning-20261123")
d = json.loads((b / "grader-layer-index.json").read_bytes())


def vol(pv):
    return (
        Path(
            subprocess.check_output(
                ["kubectl", "get", "pv", pv, "-o", "jsonpath={.spec.local.path}"], text=True
            )
        )
        / "docker/registry/v2"
    )


auth = vol("pvc-45304a2e-2d17-454e-b264-689fed7b485c")
mirror = vol("pvc-28ba4a8a-4622-47e7-b4db-fc1a2daa80a2")
p = Path("/opt/swebench/runs")
runs = {"init": p / "init50-gpt62-tuned-20261123", "mini": p / "mini50-tuned-c-gpt62-20261123"}
ids = {n: set(json.loads((r / "run.json").read_bytes())["instance_ids"]) for n, r in runs.items()}
done = {n: {f.parent.name for f in (r / "stream").glob("*/receipt.json")} for n, r in runs.items()}
eligible = {i for i in d if all(i not in ids[n] or i in done[n] for n in runs)}
refs = collections.defaultdict(set)
sizes = {}
for i, row in d.items():
    for l in row["layers"]:
        h = l["digest"][7:]
        refs[h].add(i)
        sizes[h] = l["bytes"]
found = collections.defaultdict(set)
for path in (mirror / "repositories").rglob("link"):
    if path.parent.parent.parent.name != "_layers":
        continue
    h = path.parent.name
    if h in refs:
        found[h].add(str(path.parent.parent.parent.parent.relative_to(mirror / "repositories")))
choices = {}
for h, size in sizes.items():
    a = auth / "blobs/sha256" / h[:2] / h / "data"
    m = mirror / "blobs/sha256" / h[:2] / h / "data"
    if (
        size < 2**20
        or not a.is_file()
        or not m.is_file()
        or a.stat().st_size != size
        or m.stat().st_size != size
    ):
        continue
    mapped = {d[i]["image"].split(":")[0] for i in refs[h]}
    if not found[h] or not found[h] <= mapped:
        continue
    choices[h] = {
        "size": size,
        "used_by": sorted(refs[h]),
        "all_done": refs[h] <= eligible,
        "refs_verified": True,
        "repo_count": len(found[h]),
    }
print(
    "all restricted private-backed official cache GiB",
    round(sum(x["size"] for x in choices.values()) / 2**30, 2),
)
print("finished-only GiB", round(sum(x["size"] for x in choices.values() if x["all_done"]) / 2**30, 2))
print(
    [
        (v["used_by"][:2], round(v["size"] / 2**20), v["all_done"])
        for v in sorted(choices.values(), key=lambda x: -x["size"])[:8]
    ]
)
(b / "mirror-candidates.json").write_text(json.dumps(choices, sort_keys=True))
