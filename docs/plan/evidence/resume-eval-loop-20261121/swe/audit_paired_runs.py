"""Project selected public benchmark metadata from existing, read-only SWE run artifacts.

No issue text, patches, tool arguments, private URLs or credentials are emitted.
Usage: ssh cloudnium_cloud python3 - < audit_paired_runs.py > paired-results.json
"""
import hashlib
import json
from collections import Counter
from pathlib import Path


def read_run(directory):
    report_path = directory / "report.json"
    report = json.loads(report_path.read_text())
    result, breakdown = {}, Counter()
    for item in report["instances"]:
        ident, grade = item["instance_id"], item["grading"]
        tests = {}
        grade_file = directory / "grading/logs/run_evaluation" / directory.name / report["model_name"] / ident / "report.json"
        if grade_file.exists():
            env = json.loads(grade_file.read_text())
            statuses = env[ident].get("tests_status", {})
            for tag in ("FAIL_TO_PASS", "PASS_TO_PASS"):
                if tag not in statuses:
                    continue
                status = statuses[tag]
                tests[tag] = {"passed": len(status["success"]), "failed": len(status["failure"])}
        state = grade
        if grade == "unresolved" and all(tag in tests for tag in ("FAIL_TO_PASS", "PASS_TO_PASS")):
            goal, regressions = (tests[tag]["failed"] for tag in ("FAIL_TO_PASS", "PASS_TO_PASS"))
            state = ("target_and_regression" if goal and regressions else "target_only" if goal else
                     "regression_only" if regressions else "undetermined")
        breakdown[state] += 1
        result[ident] = {k: item.get(k) for k in ("grading", "state", "final_verdict", "rounds", "cost_usd", "stop_reason", "patch_files")}
        result[ident]["official_tests"] = tests
    done = [x for x in result.values() if x["final_verdict"] == "done"]
    return result, {"report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
                    "selected": report["selected"], "resolved": report["resolved"], "grader_outcomes": dict(sorted(breakdown.items())),
                    "agent_done": len(done), "official_resolved_among_done": sum(x["grading"] == "resolved" for x in done),
                    "model_label": report["model_name"], "limits": report["limits"], "reported_usd": report["cost_usd"]}


def main():
    root = Path("/opt/swebench/runs")
    old, old_meta = read_run(root / "sol-rev18")
    new, new_meta = read_run(root / "t18-rev19")
    assert old.keys() == new.keys() and len(old) == len(new) == 50
    flips = [{"id": k, "baseline": v, "candidate": new[k]} for k, v in sorted(old.items())
             if (v["grading"] == "resolved") != (new[k]["grading"] == "resolved")]
    counts = Counter((old[k]["grading"] == "resolved", new[k]["grading"] == "resolved") for k in old)
    print(json.dumps({"baseline": "sol-rev18", "candidate": "t18-rev19",
                      "run_summary": {"sol-rev18": old_meta, "t18-rev19": new_meta},
                      "pairing": {"both_solved": counts[True, True], "became_solved": counts[False, True],
                                  "lost_including_run_error": counts[True, False], "neither_solved": counts[False, False]},
                      "flips": flips}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
