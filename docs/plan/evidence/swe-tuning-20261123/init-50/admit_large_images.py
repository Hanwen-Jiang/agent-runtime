"""Wait to admit the imminent large image pulls while existing model tasks continue."""
import json
import os
from pathlib import Path
import shutil
import signal
import time
from restore_environment import HERE, ROOT, case_state, main as restore

TARGETS=("matplotlib__matplotlib-26342","matplotlib__matplotlib-26466")
RUN=Path('/opt/swebench/runs/init50-gpt62-tuned-20261123')
def file(path):
    return json.loads(path.read_bytes())
lookup={v['id']:v['key'] for v in file(HERE/'preflight-inventory.json')}
held={}
def resume(key):
    fd=held.pop(key,None)
    if fd is None:
        return
    try:
        signal.pidfd_send_signal(fd,signal.SIGCONT)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)
def hold(iid):
    if iid in held:
        return
    for d in Path('/proc').iterdir():
        if not d.name.isdigit():
            continue
        try:
            args=d.joinpath('cmdline').read_bytes().split(b'\x00')
            if len(args)<3 or args[1]!=str(ROOT/'prewarm-tuned.py').encode() or b'--instance-id' not in args:
                continue
            if args[args.index(b'--instance-id')+1]!=iid.encode() or b'--output' not in args:
                continue
            if args[args.index(b'--output')+1]!=str(RUN/'prewarm'/(iid+'.json')).encode():
                continue
            fd=os.pidfd_open(int(d.name))
            signal.pidfd_send_signal(fd,signal.SIGSTOP)
            held[iid]=fd
            print('held pending pre-submission lease',iid,time.time(),flush=True)
            return
        except (FileNotFoundError,ProcessLookupError,PermissionError):
            continue
def stop(*args):
    raise SystemExit
signal.signal(signal.SIGTERM,stop)
signal.signal(signal.SIGINT,stop)
def main():
    # Prevent concurrent upload of the same immutable digest. Other images remain queued.
    pid=int((HERE/'warm-next.pid').read_text().strip())
    ahead=os.pidfd_open(pid)
    assert b'warm_next.py' in Path(f'/proc/{pid}/cmdline').read_bytes()
    signal.pidfd_send_signal(ahead,signal.SIGSTOP)
    deadline=time.monotonic()+25*60
    try:
        while time.monotonic()<deadline:
            if (RUN/'quota-stop.json').exists():
                break
            states={i:case_state(lookup[i])['state'] for i in TARGETS}
            for i,state in states.items():
                if state=='deleted':
                    hold(i)
                else:
                    resume(i)
            if all(v!='deleted' for v in states.values()):
                break
            free=shutil.disk_usage('/').free
            if free>=55*2**30:
                i=next(i for i in TARGETS if states[i]=='deleted')
                restore(i,minimum_free=52*2**30)
            time.sleep(2)
    finally:
        for i in list(held):resume(i)
        try:signal.pidfd_send_signal(ahead,signal.SIGCONT)
        finally:os.close(ahead)
if __name__=='__main__':main()
