"""Persist isolated gateway's metering metadata only; never retain request/response text."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ALLOW = ("tenant_id", "task_id", "node_id", "role", "method", "model_profile_id", "tier", "vendor",
         "input_tokens", "output_tokens", "cache_read_tokens", "cost_usd_micros", "status", "error_code",
         "usage_missing", "registry_scope", "registry_revision", "registry_document_sha256")
p=argparse.ArgumentParser();p.add_argument("out",type=Path);a=p.parse_args()
os.umask(0o077)
seen=set()
if a.out.is_file():
    for line in a.out.open():
        try:seen.add(json.loads(line)["event"])
        except (ValueError,KeyError):raise SystemExit("existing meter file is malformed; leave it intact")
while True:
    try:
        command=["kubectl","-n","agent-runtime","get","pods","-l","agentruntime.io/experiment=mini50-1121,agentruntime.io/role=gateway","-o","json"]
        pods=json.loads(subprocess.check_output(command))["items"]
        pod=next(x for x in pods if x["status"].get("phase")=="Running" and not x["metadata"].get("deletionTimestamp"))
        name,uid=pod["metadata"]["name"],pod["metadata"]["uid"]
        proc=subprocess.Popen(["kubectl","-n","agent-runtime","logs",name,"--timestamps","-f","--tail=-1"],stdout=subprocess.PIPE,text=True)
        with a.out.open("a",buffering=1) as out:
            for line in proc.stdout:
                ts,_,raw=line.partition(" ")
                try:j=json.loads(raw)
                except (ValueError,TypeError):continue
                if not isinstance(j,dict) or j.get("tenant_id") not in ("evals","swebench") or not j.get("model_profile_id") or not j.get("task_id"):continue
                ev=hashlib.sha256((uid+line).encode()).hexdigest()
                if ev in seen:continue
                seen.add(ev)
                out.write(json.dumps({"event":ev,"at":ts,"gateway_pod_uid":uid,**{k:j[k] for k in ALLOW if k in j}},separators=(",",":"))+"\n")
        proc.wait()
    except (OSError,subprocess.SubprocessError,StopIteration,ValueError) as exc:
        print("will retry meter read:",type(exc).__name__,file=sys.stderr,flush=True)
    time.sleep(2)
