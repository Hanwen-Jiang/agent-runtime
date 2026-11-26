"""Reconcile a quota retry with its frozen 50 IDs without overwriting the original report."""
import argparse
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_text())


def summarize(arm, retry_dir):
    original = read(ROOT / "final" / f"mini50-{arm}-report.json")
    causes = read(ROOT / "final/archive-diagnosis.json")["records"][original["run_id"]]["rows"]
    expected = {r["instance_id"]: r["task_id"] for r in causes if r["reason"] == "insufficient_user_quota"}
    protocol = read(retry_dir / "recovery-protocol.json")
    if protocol["arm"] != arm or expected != protocol["original_tasks"] or set(protocol["ids"]) != expected.keys():
        raise ValueError("Retry population differs from the original quota-interrupted tasks")
    official, = (retry_dir / "grading").glob("*.json")
    grade = read(official)
    solved = {r["instance_id"] for r in original["instances"] if r["grading"] == "resolved"}
    recovered = set()
    if arm == "b":
        recovered.update(read(ROOT / "final/b-12209-recovered-grader.json")["resolved_ids"])
    retried = set(grade["resolved_ids"])
    tested = set(grade["completed_ids"])
    unresolved = set(grade["unresolved_ids"])
    if (retried & unresolved or retried | unresolved != tested or not tested <= expected.keys()
            or len(tested) != grade["completed_instances"] or len(retried) != grade["resolved_instances"]
            or len(unresolved) != grade["unresolved_instances"]):
        raise ValueError("Official retry counts and per-issue verdicts disagree")
    if not retried <= expected.keys() or (retried & (solved | recovered)) or (solved & recovered):
        raise ValueError("Unexpected or duplicate solved issue")
    records = [read(p) for p in (retry_dir / "records").glob("*.json")]
    recorded_ids = [r["instance_id"] for r in records]
    if len(set(recorded_ids)) != len(records) or not set(recorded_ids) <= expected.keys():
        raise ValueError("Unexpected or duplicate recovery record")
    tasks = {r["instance_id"]: r["task_id"] for r in records if r.get("task_id")}
    if len(set(tasks.values())) != len(tasks) or set(tasks.values()).intersection(expected.values()):
        raise ValueError("Unexpected or reused retry task")
    if not tested <= set(grade["submitted_ids"]) <= tasks.keys():
        raise ValueError("Graded retry has no associated Task")
    return {
        "original_run": original["run_id"], "original_resolved": len(solved),
        "same_patch_regrade_resolved_ids": sorted(recovered), "retry_run": retry_dir.name,
        "retry_selected": len(expected), "retry_task_count": len(tasks),
        "retry_completed_by_grader": grade["completed_instances"],
        "all_selected_have_test_verdict": tested == expected.keys(),
        "retry_without_test_verdict_ids": sorted(expected.keys() - tested),
        "retry_record_states": dict(Counter(r.get("state", "preparing") for r in records)),
        "retry_resolved_ids": sorted(retried),
        "cumulative_distinct_resolved": len(solved | recovered | retried),
        "original_denominator": original["selected"], "first_attempt_score": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arm", choices=("a", "b"))
    parser.add_argument("retry_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.arm, args.retry_dir), indent=2))
