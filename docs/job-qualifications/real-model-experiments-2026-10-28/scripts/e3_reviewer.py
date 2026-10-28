"""E3：Reviewer 样本逐条对比：各变体给出的 verdict 与期望（取自失败信息），稳定的错误与模型之间的差异。"""

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
root = Path(sys.argv[1])
variants = sys.argv[2:]
rows: dict[str, dict[str, str]] = {}
for v in variants:
    [path] = sorted((root / v).glob("task-loop--worker--dev--*.json"))
    d = json.loads(path.read_text("utf-8"))
    for c in d["cases"]:
        if c["kind"] != "reviewer":
            continue
        msg = "；".join(f"{f['code']}:{f['message']}" for f in c["failures"]) or "✓"
        rows.setdefault(c["id"], {})[v] = msg
for cid in sorted(rows):
    print(cid)
    for v in variants:
        print(f"   {v:<15} {rows[cid].get(v, '-')[:150]}")
