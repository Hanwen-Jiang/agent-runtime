"""Verify canonical coverage and production-Docker receipts before retiring the SWE tuning services."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path("/opt/swebench/cache/swe-tuning-20261123")
RUNS = Path("/opt/swebench/runs")
CASES = {
    "init50-gpt62-tuned-20261123": ROOT / "init-50/init-50.ids",
    "mini50-tuned-c-gpt62-20261123": Path("/opt/swebench/cache/mini50-20261121-runtime-arm/mini50.ids"),
}
# Four receipts from the original main-socket-only scorer, before docker_host was recorded.
LEGACY_MAIN = {
    "astropy__astropy-8707": "01b9b3f015949ba97f20589d8088ae0e9d421adbae1006700b907cfd373a8796",
    "django__django-11790": "c5a9a5828e14f59414cd984fa05b95b17a7a566dd35280198e58f0ecac654775",
    "django__django-12050": "abd733cad33db87417e4a438368964e347c675ee666183818b5eb069f33f0e33",
    "django__django-12262": "9f0d5de48ebd8186bcb322e1197067b4ab1446470ce9511053f0e98957b1904a",
}


def read(path):
    return json.loads(path.read_bytes())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def verdicts(grade, expected, reviewed=None):
    fields = ("submitted", "completed", "empty_patch", "resolved", "unresolved")
    sets = {}
    for field in fields:
        ids = grade[field + "_ids"]
        values = set(ids)
        require(len(ids) == len(values), f"duplicate {field} IDs")
        sets[field] = values
    require(sets["submitted"] == expected, "official submission does not match selection")
    require(sets["completed"].isdisjoint(sets["empty_patch"]), "empty patch marked completed")
    require(sets["completed"] | sets["empty_patch"] == expected, "missing or extra official outcome")
    require(sets["resolved"].isdisjoint(sets["unresolved"]), "overlapping official verdicts")
    require(sets["resolved"] | sets["unresolved"] == sets["completed"], "unaccounted completed outcome")
    for field in ("error_ids", "infra_failure_ids", "incomplete_ids"):
        require(not grade.get(field), f"official report has {field}")
    for iid in grade.get("ambiguous_failure_ids", []):
        require(
            iid in sets["unresolved"]
            and (reviewed or {}).get(iid) == grade.get("failure_reasons", {}).get(iid)
            and iid in (reviewed or {}),
            f"unexplained official annotation for {iid}",
        )
    return sets


def reviewed_annotations(run):
    if run.name != "init50-gpt62-tuned-20261123":
        return {}
    note = read(ROOT / "init-50/pytest-7205-ambiguity-audit.json")
    iid = note["instance_id"]
    results = list((run / "grading").rglob(iid + "/report.json"))
    require(len(results) == 1, f"missing audited tests for {iid}")
    raw = results[0]
    tests = read(raw)[iid]
    log = raw.with_name("test_output.txt")
    require(
        digest(raw) == note["grader_test_report_sha256"] and digest(log) == note["test_output_sha256"],
        f"test annotation proof changed for {iid}",
    )
    require(
        not tests["infra_failure"]
        and not tests["resolved"]
        and tests["patch_successfully_applied"]
        and len(tests["tests_status"]["FAIL_TO_PASS"]["failure"]) == 9
        and len(tests["tests_status"]["PASS_TO_PASS"]["success"]) == 16,
        f"annotation proof invalid for {iid}",
    )
    return {iid: note["official_annotation"]}


def check(run):
    manifest = read(run / "run.json")
    selected = CASES[run.name].read_text().splitlines()
    ids = set(selected)
    require(
        len(ids) == len(selected) == 50 and selected == manifest["instance_ids"], "changed frozen cohort"
    )
    report = read(run / "report.json")
    require(
        report.get("graded") and report.get("selected") == 50 and report["run_id"] == run.name,
        "original report pending",
    )
    reports = list((run / "grading").glob("*.json"))
    require(len(reports) == 1, "expected exactly one final official report")
    grade = read(reports[0])
    reviewed = reviewed_annotations(run)
    sets = verdicts(grade, ids, reviewed)
    require(report["resolved"] == len(sets["resolved"]), "native and official summary disagree")
    predictions = [json.loads(line) for line in (run / "predictions.jsonl").read_text().splitlines()]
    by_case = {row["instance_id"]: row for row in predictions}
    require(len(predictions) == 50 and set(by_case) == ids, "missing or duplicated predictions")
    require(
        {p.parent.name for p in (run / "stream").glob("*/receipt.json")} == ids,
        "missing or unexpected stream receipts",
    )
    legacy = []
    task_ids = set()
    for iid in selected:
        record = read(run / "records" / (iid + ".json"))
        prediction = by_case[iid]
        patch = prediction["model_patch"]
        require(
            record["state"] == "finished"
            and record["task_finished"] is True
            and record["task_id"].startswith("tsk_"),
            f"missing terminal Task for {iid}",
        )
        require(
            record["task_id"] not in task_ids and record["patch"] == patch,
            f"duplicated Task or mismatched patch for {iid}",
        )
        task_ids.add(record["task_id"])
        folder = run / "stream" / iid
        receipt_path = folder / "receipt.json"
        receipt = read(receipt_path)
        require(
            receipt["instance_id"] == iid and receipt["run_id"] == run.name,
            f"receipt identity mismatch for {iid}",
        )
        require(
            receipt["model"] == prediction["model_name_or_path"] == manifest["settings"]["model_name"],
            f"model mismatch for {iid}",
        )
        socket = receipt.get("docker_host")
        if socket is None and LEGACY_MAIN.get(iid) == digest(receipt_path):
            legacy.append(iid)
        else:
            require(socket == "unix:///var/run/docker.sock", f"non-production grader for {iid}")
        require(
            receipt["patch_sha256"] == hashlib.sha256(patch.encode()).hexdigest(), f"wrong patch for {iid}"
        )
        raw = folder / f"{receipt['model']}.{run.name}.json"
        require(digest(raw) == receipt["official_report_sha256"], f"unbound grader result for {iid}")
        per_case = verdicts(read(raw), {iid}, reviewed)
        for field in ("completed", "resolved", "unresolved", "empty_patch"):
            require(
                (iid in per_case[field]) == (iid in sets[field]), f"graded stage mismatch for {iid}/{field}"
            )
        require(receipt["resolved"] == (iid in sets["resolved"]), f"receipt verdict mismatch for {iid}")
        empty = iid in sets["empty_patch"]
        require(
            receipt["empty_patch"] == empty and (not patch.strip()) == empty,
            f"empty-patch mismatch for {iid}",
        )
        if empty:
            require(
                not record.get("patch_error") and not record.get("error"),
                f"unexplained empty patch for {iid}",
            )
    return {
        "run_id": run.name,
        "selection_sha256": digest(CASES[run.name]),
        "report_sha256": digest(run / "report.json"),
        "official_grade_sha256": digest(reports[0]),
        "predictions_sha256": digest(run / "predictions.jsonl"),
        "unique_tasks": len(task_ids),
        "production_docker_receipts": len(ids),
        "legacy_main_receipts": legacy,
        "reviewed_official_annotations": {
            iid: reviewed[iid] for iid in grade.get("ambiguous_failure_ids", [])
        },
        "resolved": len(sets["resolved"]),
        "unresolved": len(sets["unresolved"]),
        "completed": len(sets["completed"]),
        "empty_patch_ids": sorted(sets["empty_patch"]),
    }


if __name__ == "__main__":
    print(
        json.dumps(
            {
                "verified_utc": datetime.now(timezone.utc).isoformat(),
                "runs": [check(RUNS / n) for n in CASES],
            },
            indent=2,
        )
    )
