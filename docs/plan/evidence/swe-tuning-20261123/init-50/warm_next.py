"""Rehydrate a small look-ahead window as the native init-50 runner advances."""
from pathlib import Path
import json
import subprocess
import time
import fcntl
from restore_environment import HERE, ROOT, main as restore

RUN=Path('/opt/swebench/runs/init50-gpt62-tuned-20261123')
ids=(HERE/'init-50.ids').read_text().splitlines()
assert len(ids)==len(set(ids))==50
with (HERE/'warm-ahead.lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    while not (RUN/'report.json').exists():
        seen={p.stem for p in (RUN/'records').glob('*.json')}
        remaining=[i for i in ids if i not in seen]
        if not remaining or (RUN/'quota-stop.json').exists():
            break
        subprocess.run(['python3',str(HERE/'trim_runtime_cache.py'),str(RUN),str(HERE/'preflight-inventory.json')],check=False)
        for i in remaining[:5]:
            try:
                restore(i)
            except (ValueError,OSError,RuntimeError,AssertionError,subprocess.SubprocessError) as e:
                print('history unavailable; native build remains eligible:',i,type(e).__name__,flush=True)
        time.sleep(40)
