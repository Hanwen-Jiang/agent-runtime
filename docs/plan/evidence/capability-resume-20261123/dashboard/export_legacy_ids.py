"""Offline projection for the historical public matrix. Never copy prompts, results or private IDs."""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import tempfile

root = Path("/opt/evals/mx")
public = {"bfcl-v4", "judgebench", "rewardbench2"}
for name in ("round1", "bfcl-paired-20261121T103248Z", "bfcl-paired-20261121T103248Z-control"):
    d = root/name
    scores, plan = (d/"scores.json").read_bytes(), (d/"plan.json").read_bytes()
    plan_obj, score_obj = json.loads(plan), json.loads(scores)
    assert plan_obj["name"] == score_obj["plan"] == name
    selected, versions, rows = defaultdict(set), {}, Counter()
    with (d/"results.jsonl").open() as source:
        for line in source:
            x = json.loads(line)
            col = f"{x['task_family']}/{x['role']}.{x['tier']}"
            rows[col, x["candidate"]] += 1
            if x["eval"] not in public:
                continue
            iid = x["item"]
            assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.: -]{0,127}", iid)
            k = (col, x["candidate"], x["eval"])
            selected[k].add(iid)
            assert versions.setdefault(k, x["version"]) == x["version"]
    projections = {}
    for cell in score_obj["cells"]:
        col = f"{cell['task_family']}/{cell['role']}.{cell['tier']}"
        by_eval = {e["name"]:e["version"] for e in cell["evals"]}
        for model in cell["candidates"]:
            mid = model["candidate"]
            assert rows[col, mid] == model["runs"]
            for ev,ver in by_eval.items():
                k = (col, mid, ev)
                if k not in selected:
                    continue
                assert ver == versions[k]
                projections.setdefault(col, {}).setdefault(mid, {})[ev] = {"version":ver, "ids":sorted(selected[k])}
    doc = {"source_sha256":hashlib.sha256(scores+b"\0"+plan).hexdigest(), "observations":projections}
    with tempfile.NamedTemporaryFile(mode="w", dir=d, delete=False) as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",",":"))
        tmp = Path(f.name)
    tmp.chmod(0o644)
    tmp.replace(d/"public-cohorts.json")
    print(name, sum(len(ids) for ids in selected.values()), len(selected))
