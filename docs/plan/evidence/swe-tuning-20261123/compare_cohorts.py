"""Compare the two full C runs independently against their predeclared historical grades."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_bytes())


def compare(folder, baseline, frozen, protocol):
    run = read(folder / "run.json")
    report = read(folder / "report.json")
    official_path = folder / "grading" / f"{report['model_name']}.{run['run_id']}.json"
    official = read(official_path)
    ids = run["instance_ids"]
    if ids != frozen or len(set(ids)) != 50 or set(ids) != set(baseline):
        raise ValueError("Cohort mismatch")
    if protocol["run_id"] != run["run_id"] or protocol["runtime_tree"] != run["selection"]["runtime_tree"]:
        raise ValueError("Candidate runtime mismatch")
    if report["run_id"] != run["run_id"] or report["selected"] != len(ids) or not report["graded"]:
        raise ValueError("Missing final run report")
    rows = {r["instance_id"]:r for r in report["instances"]}
    solved = set(official["resolved_ids"])
    unresolved = set(official["unresolved_ids"])
    if set(rows) != set(ids) or solved & unresolved or set(official["completed_ids"]) != solved | unresolved:
        raise ValueError("Invalid case coverage")
    flags = {k:set(official.get(k + "_ids", [])) for k in ("empty_patch","error","infra_failure","ambiguous_failure")}
    if set(official["submitted_ids"]) != set(ids) or solved | unresolved | flags["empty_patch"] != set(ids):
        raise ValueError("Some selected cases do not yet have a final harness disposition")
    if flags["empty_patch"] & (solved | unresolved):
        raise ValueError("Empty patch was also assigned a binary verdict")
    # The harness can list the same patch as both unresolved and ambiguous.
    # Preserve both raw fields and use the adapter's priority for the public grade.
    flagged_unresolved = unresolved.intersection(set().union(*flags.values()))
    for label, expected in (("resolved",solved), ("unresolved",unresolved-flagged_unresolved)):
        if expected != {i for i, r in rows.items() if r["grading"] == label}:
            raise ValueError("Per-case verdict differs from official harness")
    counts = Counter(r["grading"] for r in rows.values())
    before = {i for i,r in baseline.items() if r["grade"] == "resolved"}
    pairs = {k:[] for k in ("both_resolved","newly_resolved","newly_unresolved","both_unresolved",
                              "previously_ungraded","candidate_ungraded")}
    for i in ids:
        prev, new = baseline[i]["grade"], rows[i]["grading"]
        if new not in ("resolved","unresolved"):
            key = "candidate_ungraded"
        elif prev not in ("resolved","unresolved"):
            key = "previously_ungraded"
        elif prev == new:
            key = "both_" + new
        else:
            key = "newly_" + new
        pairs[key].append(i)
    return {"run_id":run["run_id"], "model_profile":protocol["model_profile"], "runtime_tree":protocol["runtime_tree"],
            "selected":len(ids), "graded":counts["resolved"]+counts["unresolved"], "resolved":len(solved),
            "harness_completed":len(official["completed_ids"]),
            "harness_flags":{k:sorted(v) for k,v in flags.items() if v},
            "baseline_resolved":len(before),
            "only_candidate_resolved":[i for i in ids if i in solved-before],
            "only_baseline_resolved":[i for i in ids if i in before-solved],
            "per_case_grades":{i:rows[i]["grading"] for i in ids}, "outcomes":dict(counts), "paired":pairs,
            "reported_cost_usd":report["cost_usd"],
            "official_sha256":hashlib.sha256(official_path.read_bytes()).hexdigest()}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("runs",type=Path,help="Directory containing both native run folders")
    p.add_argument("evidence",type=Path,help="Directory containing baselines.json and freeze.json")
    args = p.parse_args()
    baselines = read(args.evidence / "baselines.json")
    freeze = read(args.evidence / "freeze.json")
    result = {}
    for batch, rid, ids_path in [
        ("init-50","init50-gpt62-tuned-20261123",args.evidence / "init-50" / "init-50.ids"),
        ("mini-50","mini50-tuned-c-gpt62-20261123",args.evidence.parent / "swe-mini50-20261121" / "mini50.ids"),
    ]:
        folder = args.runs / rid
        protocol = read(folder / "paired-protocol.json")
        if (protocol["runtime_tree"],protocol["model_profile"],protocol["tier"]) != (
            freeze["candidate_tree"],freeze["model_profile"],freeze["tier"]):
            raise ValueError("Unexpected runtime, model profile or model tier")
        result[batch] = compare(folder, baselines[batch]["cases"], ids_path.read_text().splitlines(), protocol)
    print(json.dumps(result, ensure_ascii=False, indent=2))
