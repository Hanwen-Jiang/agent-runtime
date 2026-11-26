"""Update a read-only combined progress view while the three paid cohorts run."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from merge import merge
from supplemental import DEPLOY,NAMES,NATIVE,PREFIX,SUPP,read


def signatures(paths):
    out=[]
    for p in paths:
        try:
            a=p.stat()
            out.append((a.st_size,a.st_mtime_ns))
        except FileNotFoundError:
            out.append(None)
    return tuple(out)


if __name__=='__main__':
    sigs={}
    stop=DEPLOY/'supplemental-code'/'stop-projection'
    while not stop.exists():
        final=0
        for group in NAMES:
            dirs=[base/(PREFIX+group) for base in (NATIVE,SUPP)]
            paths=[d/n for d in dirs for n in ('results.jsonl','provider-quota-pause.json','user-pause.json')]
            doc=dirs[0]/'public-supplemental-coverage.json'
            if doc.is_file() and read(doc).get('complete') is True:
                final+=1; continue
            stamp=signatures(paths)
            if sigs.get(group)==stamp:continue
            try:
                print(datetime.now(timezone.utc).isoformat(),merge(group),flush=True)
                sigs[group]=stamp
            except BlockingIOError:
                continue
        if final==len(NAMES):break
        time.sleep(8)
