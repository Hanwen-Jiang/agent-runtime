"""E3：Reviewer 样本 10-22 的报告与今天的两次基线逐条比：哪些以前对、现在错（或反过来）。样本集合有没有变。"""

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
paths = [Path(p) for p in sys.argv[1:]]
rows: dict[str, list[str]] = {}
for i, p in enumerate(paths):
    d = json.loads(p.read_text("utf-8"))
    for c in d["cases"]:
        if c["kind"] == "reviewer":
            rows.setdefault(c["id"], ["-"] * len(paths))[i] = "✓" if c["passed"] else "✗ " + "；".join(f["message"] for f in c["failures"])[:70]
print("列：", [p.parent.name + "/" + p.name[:40] for p in paths])
for cid in sorted(rows):
    r = rows[cid]
    flag = "  ← 变化" if len({x[:1] for x in r}) > 1 else ""
    print(f"{cid:<32} " + " | ".join(x[:40] for x in r) + flag)
