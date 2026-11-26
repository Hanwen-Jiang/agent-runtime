"""Watch nodefs headroom only while these two evaluation runners use the machine."""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import release_finished_cri

ROOT = Path("/opt/swebench/cache/swe-tuning-20261123")
RUNS = (
    Path("/opt/swebench/runs/mini50-tuned-c-gpt62-20261123"),
    Path("/opt/swebench/runs/init50-gpt62-tuned-20261123"),
)
GIB = 2**30


def in_progress():
    for run in RUNS:
        for filename in ("launcher.pid", "runner.pid", "stream.pid"):
            try:
                pid = int((run / filename).read_text().strip())
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            except (OSError, ValueError):
                continue
            if os.fsencode(run.name) in cmdline:
                return True
    return False


def watch():
    journal_path = ROOT / "recoverable-swe-docker-cache/headroom.jsonl"
    with (ROOT / "disk-guard.lock").open("a") as guard, (ROOT / "grader-host.lock").open("a") as graders:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        idle_since = time.monotonic()
        previous_check = 0.0
        while time.monotonic() - idle_since < 300:
            active = in_progress()
            if active:
                idle_since = time.monotonic()
            free = shutil.disk_usage("/").free
            with journal_path.open("a") as journal:
                print(json.dumps({"epoch": int(time.time()), "free_bytes": free}), file=journal)
            if active and free < 58 * GIB:
                try:
                    fcntl.flock(graders, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    try:
                        release_finished_cri.release()
                    finally:
                        fcntl.flock(graders, fcntl.LOCK_UN)
            if active and free < 59 * GIB and time.monotonic() - previous_check >= 120:
                # Only this experiment's newly restored, duplicate, private-registry blobs
                # qualify. Protect resources still assigned to a running model.
                subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "registry_headroom.py"),
                        "share",
                        "--gib",
                        "16",
                        "--off-queue",
                        "--queue-ahead",
                        "0",
                        "--stop-free-gib",
                        "61",
                    ],
                    check=False,
                )
                previous_check = time.monotonic()
            time.sleep(30)


if __name__ == "__main__":
    watch()
