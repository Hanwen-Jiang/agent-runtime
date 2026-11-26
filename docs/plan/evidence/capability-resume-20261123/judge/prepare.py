"""Freeze the three missing judge candidates and the same checkpoints for every candidate."""
import hashlib
import json
from pathlib import Path

from agentruntime_learning.capability.evals import judgebench, rewardbench2, s10

DEPLOY = Path("/opt/evals/deployments/capability-resume-20261123")
WORK = DEPLOY / "judge"
# Selected before any judge was scored. Keep eight reviewed questions per official RB2 subcategory.
RB2_IDS = {
    "Factuality": [115, 167, 347, 290, 1, 323, 209, 124],
    "Focus": [1591, 1720, 1733, 1421, 1388, 1583, 1327, 1655],
    "Math": [677, 748, 730, 780, 651, 723, 707, 806],
    "Precise IF": [544, 501, 598, 545, 624, 603, 557, 563],
    "Safety": [1208, 1231, 1189, 1103, 1142, 1174, 1114, 1220],
}
MODELS = ["a6api/claude-opus-5-5@2026-10", "a6api/claude-fable-5-1@2026-11", "a6api/gpt-6-astra@2026-11"]


def save(path, value):
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    if path.exists() and path.read_bytes() != data:
        raise ValueError(f"Refusing to replace frozen input {path.name}")
    path.write_bytes(data)


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    rb = rewardbench2.make()
    approved = {f"{group}:{i}" for group, numbers in RB2_IDS.items() for i in numbers}
    rows = []
    for ident in sorted(approved):
        row = rb.rows()[ident]
        # The loader accepts the upstream raw ID and applies the subset prefix itself.
        prefix = row["subset"] + ":"
        assert row["id"].startswith(prefix)
        rows.append({**row, "id": row["id"][len(prefix):]})
    source = WORK / "rewardbench2-reviewed.jsonl"
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    if source.exists() and source.read_text() != text:
        raise ValueError("Reviewed source differs")
    source.write_text(text)
    selected = rewardbench2.make(source=source)
    evaluators = [s10.make(), judgebench.make(), selected]
    cohorts = {e.name:{"version":e.version, "ids":[i.id for i in e.items(40, 0)]} for e in evaluators}
    assert set(cohorts[selected.name]["ids"]) == approved
    assert all(len(set(c["ids"])) == len(c["ids"]) == 40 for c in cohorts.values())
    originals = {i.id for i in rb.items(40, 0)}
    save(WORK / "cohorts.json", {"seed":0, "n_per_evaluator":40, "repeats":3, "cohorts":cohorts,
         "rewardbench2_subsets":RB2_IDS, "rewardbench2_overlap_with_default_40":len(originals & approved),
         "rewardbench2_source_sha256":hashlib.sha256(source.read_bytes()).hexdigest()})
    save(WORK / "formal-v2.json", {"name":"capability-resume-20261123-judge-v2", "task_family":"general.unclassified",
         "n":40,"r":3,"seed":0,"concurrency":7,"item_usd_limit":8.0,
         "cells":[{"role":"judge", "tier":"medium", "evals":["s10","judgebench",
                   {"eval":"rewardbench2","options":{"source":str(source)}}], "candidates":MODELS}]})


if __name__ == "__main__":
    main()
