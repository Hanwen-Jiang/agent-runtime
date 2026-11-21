"""Run the browser red/green test and the local delivery fault matrix. No external GitHub writes."""
import argparse
import collections
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

BASE = "0cd2c890d214a53a3768568b7d80103eb0fd7341"
FAULT = "TestExpiredStreamRecoversAtBatchCursorWithoutReplayingFinishedTasks"
CONTROLS = [
    "TestDuplicatesAreNotProcessedTwiceAcrossRestartsAndReplicas",
    "TestExpiredLeasesAreReclaimed",
    "TestOneTriggerAuthorizesOnePushAndOnePullRequest",
    "TestAnInterruptedPushIsNotRetried",
    "TestLostPushAcknowledgementDoesNotRepeatTheWrite",
    "TestTheBatchIsPagedInBrowser",
    "TestTasksTheBatchCannotServeAreReplayedInBrowser",
    "TestTheFirstPaintComesFromTheSnapshotInBrowser",
]


def run_cases(repo, packages, tests, repeat):
    pattern = "^(" + "|".join(tests) + ")$"
    cmd = ["go", "test", "-count=" + str(repeat), "-p", "2", "-json", *packages, "-run", pattern]
    proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    outcomes = {name: collections.Counter() for name in tests}
    routes = []
    for line in proc.stdout.splitlines():
        row = json.loads(line)
        if row.get("Test") in outcomes and row["Action"] in ("pass", "fail", "skip"):
            outcomes[row["Test"]][row["Action"]] += 1
        if row.get("Test") == FAULT and "snapshot requests=" in row.get("Output", ""):
            routes.append(row["Output"].split("snapshot requests=", 1)[1].strip())
    if any(sum(outcome.values()) != repeat for outcome in outcomes.values()):
        raise RuntimeError("Incomplete execution: " + proc.stderr[-1500:])
    return {"go_exit": proc.returncode, "tests": outcomes, "browser_observations_after_snapshot_requests_equals": routes}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("coding_repo", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    if not os.environ.get("CODING_TEST_BROWSER"):
        parser.error("Set CODING_TEST_BROWSER to an installed Chrome/Chromium executable.")
    repo = args.coding_repo.resolve()
    with tempfile.TemporaryDirectory() as td:
        temp = Path(td)
        old = temp / "coding-agent"
        old.mkdir()
        (temp / "agent-runtime").symlink_to(repo.parent / "agent-runtime", target_is_directory=True)
        with (temp / "base.tar").open("wb") as out:
            subprocess.run(["git", "-C", str(repo), "archive", BASE], check=True, stdout=out)
        with tarfile.open(temp / "base.tar") as archive:
            archive.extractall(old, filter="data")
        test_file = Path("internal/web/session_events_test.go")
        (old / test_file).write_bytes((repo / test_file).read_bytes())
        baseline = run_cases(old, ["./internal/web"], [FAULT], args.repeat)
        candidate = run_cases(repo, ["./internal/github", "./internal/server", "./internal/web"],
                              [FAULT, *CONTROLS], args.repeat)
    files = [Path("internal/web/static/app.js"), test_file, Path("internal/server/githubtrigger_test.go")]
    data = {"executed_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "baseline_revision": BASE,
            "candidate_sha256": {str(f): hashlib.sha256((repo / f).read_bytes()).hexdigest() for f in files},
            "repeat": args.repeat, "baseline": baseline, "candidate": candidate,
            "environment": "Local Chrome, Go handlers and temporary PostgreSQL. Task, Git push and GitHub PR endpoints are test doubles."}
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    if (baseline["go_exit"] != 1 or baseline["tests"][FAULT].get("fail") != args.repeat
            or candidate["go_exit"] != 0 or any(v != {"pass": args.repeat} for v in candidate["tests"].values())):
        raise SystemExit("Unexpected result; inspect the individual observations.")


if __name__ == "__main__":
    main()
