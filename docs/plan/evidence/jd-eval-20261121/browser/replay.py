"""Reproduce the two-session fault on the previous validated JS, then the current frontend."""
import argparse
import collections
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
REV = "0cd2c890d214a53a3768568b7d80103eb0fd7341"
ASSET = "internal/web/static/app.js"
FAULT = "TestRecoveryOfNewSessionIsIndependentOfPreviousSession"
CONTROLS = ["TestExpiredStreamRecoversAtBatchCursorWithoutReplayingFinishedTasks",
            "TestTasksTheBatchCannotServeAreReplayedInBrowser", "TestTheFirstPaintComesFromTheSnapshotInBrowser",
            "TestTruncatedSnapshotBackfillsEveryProgressGroupAfterReload"]


def measure(repo, names, n):
    proc = subprocess.run(["go", "test", "-json", "-count", str(n), "./internal/web",
                           "-run", "^(" + "|".join(names) + ")$"], cwd=repo, capture_output=True, text=True)
    counts = {key: collections.Counter() for key in names}
    observations = []
    for line in proc.stdout.splitlines():
        item = json.loads(line)
        if (name := item.get("Test")) not in counts:
            continue
        if item["Action"] in ("pass", "skip", "fail"):
            counts[name][item["Action"]] += 1
        if "two sessions: " in item.get("Output", ""):
            observations.append(item["Output"].split("two sessions: ", 1)[1].strip())
    if any(sum(c.values()) != n for c in counts.values()):
        raise RuntimeError("incomplete browser run: " + proc.stderr[-500:])
    return {"exit": proc.returncode, "counts": counts, "observations": observations,
            "source_sha256": hashlib.sha256((repo/ASSET).read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("coding_agent", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = args.coding_agent.resolve()
    if not os.environ.get("CODING_TEST_BROWSER"):
        parser.error("Set CODING_TEST_BROWSER to a local Chromium or Chrome binary.")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        base = tmp / "coding-agent"
        base.mkdir()
        (tmp / "agent-runtime").symlink_to(repo.parent / "agent-runtime", target_is_directory=True)
        with (tmp / "source.tar").open("wb") as f:
            subprocess.run(["git", "-C", str(repo), "archive", REV], stdout=f, check=True)
        with tarfile.open(tmp / "source.tar") as archive:
            archive.extractall(base, filter="data")
        subprocess.run(["git", "apply", str(HERE / "prior-resnapshot.patch")], cwd=base, check=True)
        assert hashlib.sha256((base/ASSET).read_bytes()).hexdigest() == "357125e394375c339b7ff957af02832526e064c683e2c892c5e8e015363ead20"
        new_test = Path("internal/web/session_recovery_race_test.go")
        shutil.copy2(repo / new_test, base / new_test)
        baseline = measure(base, [FAULT], 5)
        candidate = measure(repo, [FAULT, *CONTROLS], 5)
    data = {"date_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "git_base": REV,
            "protocol": "Chromium sessions A and B, hold A recovery while B receives resnapshot_required; first attempt per repetition",
            "public_case_count": 1, "repetitions_per_case": 5, "separate_control_case_count": len(CONTROLS),
            "fixture_sha256": hashlib.sha256((repo/new_test).read_bytes()).hexdigest(),
            "baseline": baseline, "candidate": candidate}
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n")
    assert baseline["counts"][FAULT] == {"fail": 5} and candidate["exit"] == 0
    assert all(c == {"pass": 5} for c in candidate["counts"].values())


if __name__ == "__main__":
    main()
