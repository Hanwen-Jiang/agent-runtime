"""Run real local commands, then feed their observations to the Runtime evidence recorder and Reviewer.

Use the repository's .venv; the Reviewer fixtures provide a fixed model proposal, not hidden tests.
The baseline is the committed recorder at --baseline, the candidate is the working-tree recorder.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
WORKER = ROOT / "services/py-worker"
RECORDER = "services/py-worker/src/agentruntime_worker/workflows/verification.py"
sys.path.insert(0, str(WORKER))
from tests.test_tuning_done_evidence import decide, done
from agentruntime_worker.workflows.verification import VerificationRecord


def load_baseline(ref: str, tmp: Path):
    source = subprocess.check_output(["git", "show", f"{ref}:{RECORDER}"], cwd=ROOT)
    path = tmp / "original_recorder.py"
    path.write_bytes(source)
    spec = importlib.util.spec_from_file_location("original_recorder", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.VerificationRecord, hashlib.sha256(source).hexdigest()


def observe(argv, cwd, timeout=10):
    try:
        proc = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=timeout)
        return {"exit_code": proc.returncode, "timed_out": False, "stdout": proc.stdout, "stderr": proc.stderr}
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "timed_out": True, "stdout": "", "stderr": ""}


def review(cls, steps, observations, *, path="case.py"):
    rec = cls()
    rec.offered({"repo_edit": "write_reversible", "shell_run": "write_reversible"})
    rec.called("repo_edit", "write_reversible", {"path": path}, {"ok": True})
    for name in steps:
        argv, obs = observations[name]
        rec.called("shell_run", "write_reversible", {"argv": argv}, obs)
    decision = decide(done("report"), rec.as_dict()).verdict
    return {"verdict": decision["verdict"], "reason_code": decision.get("reason_code"),
            "ran_after_write": rec.ran_after_write, "last_exit_code": rec.last_exit_code}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", default="da58416909fc54f02519de95706df01d9bbd1701")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp)
        baseline, source_hash = load_baseline(args.baseline, p)
        (p / "empty").mkdir()
        (p / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_add(self): self.assertEqual(1+1, 2)\n")
        (p / "test_bad.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_add(self): self.assertEqual(1+1, 3)\n")
        py = sys.executable
        commands = {
            "empty_pytest": ([py, "-m", "pytest", "-q", "empty"], 10),
            "all_deselected": ([py, "-m", "pytest", "-q", "test_ok.py", "-k", "missing_case"], 10),
            "empty_unittest": ([py, "-m", "unittest", "discover", "-s", "empty"], 10),
            "timed_out": ([py, "-c", "import time; time.sleep(10)"], 0.15),
            "passing_pytest": ([py, "-m", "pytest", "-q", "test_ok.py"], 10),
            "collect_pytest": ([py, "-m", "pytest", "--collect-only", "-q", "test_ok.py"], 10),
            "collect_pytest_short": ([py, "-m", "pytest", "--co", "-q", "test_ok.py"], 10),
            "failing_pytest": ([py, "-m", "pytest", "-q", "test_bad.py"], 10),
            "passing_unittest": ([py, "-m", "unittest", "test_ok"], 10),
            "purposeful_check": ([py, "-c", "assert 1+1==2"], 10),
        }
        observations = {k: (argv, observe(argv, p, timeout)) for k, (argv, timeout) in commands.items()}
        cases = [
            ("empty_pytest", ["empty_pytest"], "continue", None),
            ("selected_no_tests", ["all_deselected"], "continue", None),
            ("empty_unittest", ["empty_unittest"], "continue", None),
            ("timeout", ["timed_out"], "continue", None),
            ("collect_only", ["collect_pytest"], "continue", None),
            ("collect_short", ["collect_pytest_short"], "continue", None),
            ("failed_then_collect", ["failing_pytest", "collect_pytest"], "done", 1),
            ("passing_pytest", ["passing_pytest"], "done", 0),
            ("failed_tests_ran", ["failing_pytest"], "done", 1),
            ("passing_unittest", ["passing_unittest"], "done", 0),
            ("other_valid_check", ["purposeful_check"], "done", 0),
            ("empty_then_valid", ["empty_pytest", "passing_pytest"], "done", 0),
            ("failed_then_empty", ["failing_pytest", "empty_pytest"], "done", 1),
            ("pass_then_timeout", ["passing_pytest", "timed_out"], "done", 0),
            ("doc_only", [], "done", None),
        ]
        groups = {}
        for label, cls in [("baseline", baseline), ("candidate", VerificationRecord)]:
            rows = []
            for case, steps, expected, code in cases:
                out = review(cls, steps, observations, path="guide.md" if case == "doc_only" else "case.py")
                rows.append({"case": case, **out, "expected_verdict": expected, "expected_exit": code,
                             "pass": (out["verdict"], out["last_exit_code"]) == (expected, code)})
            groups[label] = {"passed": sum(r["pass"] for r in rows), "total": len(rows), "rows": rows}
        result = {"subject": "VerificationRecord + deterministic Reviewer enforcement with a fixed done proposal",
                  "baseline_commit": args.baseline, "baseline_recorder_sha256": source_hash,
                  "candidate_recorder_sha256": hashlib.sha256((ROOT / RECORDER).read_bytes()).hexdigest(),
                  "actual_commands": {k: {"exit_code": x["exit_code"], "timed_out": x["timed_out"]} for k, (_, x) in observations.items()},
                  **groups}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return int(groups["candidate"]["passed"] != groups["candidate"]["total"])


if __name__ == "__main__":
    raise SystemExit(main())
