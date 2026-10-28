"""E2：给网关做三份模型清单，只换 strong 档（Planner、Reviewer 用 strong@medium），其余档不动。"""

import copy
import json
import sys
from pathlib import Path

src = Path(sys.argv[1])
out = Path(sys.argv[2])
base = json.loads(src.read_text("utf-8"))
out.mkdir(parents=True, exist_ok=True)


def keep(strong_id: str | None, promote: str | None = None) -> dict:
    doc = copy.deepcopy(base)
    profiles = []
    for p in doc["profiles"]:
        if p["class"] == "strong" and p["id"] != strong_id:
            continue
        if promote and p["id"] == promote:
            # 降档实验：mid 档的模型登记成 strong@medium（不带推理强度），看 Planner、Reviewer 换成它能不能守住门槛。
            p["class"] = "strong"
            p["tiers"] = [{"name": "medium", "effort": "", "thinking_budget": 0}]
        profiles.append(p)
    doc["profiles"] = profiles
    return doc


variants = {
    "gpt55": keep("a6api/gpt-5.5@2026-10"),
    "opus": keep("a6api/claude-opus-5-5@2026-10"),
    "geminiflash": keep(None, promote="a6api/gemini-3.5-flash@2026-10"),
}
for name, doc in variants.items():
    (out / f"profiles-{name}.json").write_text(json.dumps(doc, indent=2), "utf-8")
    strong = [p["id"] for p in doc["profiles"] if p["class"] == "strong"]
    mid = [p["id"] for p in doc["profiles"] if p["class"] == "mid"]
    print(name, "strong:", strong, "mid:", mid)
