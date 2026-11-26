"""Restore this run's secondary SWE cache when both original 50/50 graders have closed."""

import fcntl
from pathlib import Path
import subprocess
import sys
import time

from completion_gate import check

base = Path(__file__).resolve().parent
run_root = Path("/opt/swebench/runs")
runs = [run_root / name for name in ("init50-gpt62-tuned-20261123", "mini50-tuned-c-gpt62-20261123")]


def completed(run):
    try:
        check(run)
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return False
    return True


def main():
    with (base / "mirror-final-restore.lock").open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for _ in range(36 * 60):
            if all(completed(run) for run in runs):
                for operation in ("mirror_headroom.py", "registry_headroom.py"):
                    result = subprocess.run(
                        [sys.executable, str(base / operation), "restore"], capture_output=True, text=True
                    )
                    if result.returncode:
                        print((result.stderr or result.stdout).splitlines()[-1], flush=True)
                        break
                    print(result.stdout.strip(), flush=True)
                else:
                    return 0
            time.sleep(60)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
