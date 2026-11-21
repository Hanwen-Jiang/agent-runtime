"""Gate native SWE SubmitTask on the real, read-only official project-environment lease.

Run on the authorized cloudnium evaluator host after native CreateWorkspace and before
SubmitTask. This does not create workspaces, submit tasks, execute shell commands in
them, mutate registry rows, or export service credentials.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,110}")
IID = re.compile(r"[a-z0-9_-]+__[a-z0-9_-]+-[0-9]{1,10}")
SHA = re.compile(r"sha256:[a-f0-9]{64}")

POD = """
import asyncio, datetime, hashlib, json, os, re, sys
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.tools import sandbox_broker
from agentruntime_core.tenancy import TenantContext
from agentruntime_core.providers.sandbox import SandboxSpec
async def run():
    iid, owner, user, workspace, request_timeout = sys.argv[1:]
    broker=sandbox_broker(WorkerConfig.from_env(os.environ))
    if broker is None: return {"error": "broker_unavailable"}
    tenant=TenantContext(tenant_id=owner,user_id=user,workspace_id=workspace)
    # No task/session association: never count this diagnostic lease as official
    # model-generated shell activity. Never share the running task's writable lease.
    spec=SandboxSpec(runtime="universal",workspace_id=workspace,idle_timeout=datetime.timedelta(seconds=120))
    lease=None
    data={}
    try:
        lease=await asyncio.wait_for(broker.acquire(tenant,spec,writable=False,reuse=False,prepare=False,
                                                   project_environment=True),timeout=float(request_timeout))
        env=lease.environment or {}
        data.update(state=str(env.get("state","none")), kind=env.get("kind"), path=env.get("definition"),
                    runtime_image_id=lease.record.image, backend=lease.backend.name,
                    writable=lease.record.writable, lease_in_workspace=lease.record.workspace_id==workspace)
        if data["state"] in ("ready","building"):
            dev_name=".devcontainer/devcontainer.json"
            df_name=".devcontainer/agentruntime.Dockerfile"
            dev = await asyncio.wait_for(lease.box.read_file(lease.env_id,dev_name),timeout=15)
            df = await asyncio.wait_for(lease.box.read_file(lease.env_id,df_name),timeout=15)
            cfg=json.loads(dev);build=cfg.get("build",{})
            data["definition_matches"]=(env.get("kind")=="devcontainer" and env.get("definition")==dev_name
                    and build.get("dockerfile")=="agentruntime.Dockerfile" and build.get("context",".")=="."
                    and df.decode().splitlines()[0]=="FROM swebench/sweb.eval.x86_64."+iid.replace("__","_1776_").lower()+":latest")
            data.update(content_sha256=hashlib.sha256(dev).hexdigest(),dockerfile_sha256=hashlib.sha256(df).hexdigest())
    except Exception as e:
        data["error"]=type(e).__name__+":"+str(getattr(e,"code","unknown"))[:90]
    finally:
        if lease is not None:
            try:
                await asyncio.wait_for(broker.release(lease,keep=False,skip_snapshot=True),timeout=30)
                data["released"]=True
            except Exception as e:
                data["released"]=False;data["error"]="release:"+type(e).__name__
    return data
print(json.dumps(asyncio.run(run())),flush=True)
"""


def stamp():
    return datetime.now(timezone.utc).isoformat()


def command(argv, timeout, data=None):
    done=subprocess.run(argv,input=data,capture_output=True,text=True,timeout=timeout)
    if done.returncode:
        raise RuntimeError(f"{argv[0]} exited {done.returncode}")
    return done.stdout


def registry_row(owner, key):
    # Both inputs validated; no raw Dockerfiles, sensitive data or credentials in outputs.
    sql=(f"BEGIN READ ONLY; SELECT state,image,digest,ready_at,build_ms FROM sandbox_images "
         f"WHERE tenant_id='{owner}' AND image_key='{key}'; COMMIT;")
    val=command(["kubectl","-n","data","exec","-i","pg-1","-c","postgres","--","psql",
                 "-d","agentruntime","-v","ON_ERROR_STOP=1","-At"],20,sql)
    rows=[r.split("|") for r in val.splitlines() if "|" in r]
    if len(rows)!=1:return {"state":"not_registered"}
    state,image,digest,at,ms=rows[0]
    return {"state":state,"image":image,"digest":digest,"ready_at":at,"build_ms":int(ms) if ms.isdigit() else None}


def current_base_digest():
    cfg=json.loads(command(["kubectl","-n","agent-runtime","get","cm","ar-agent-runtime-sandbox-backends","-o","json"],20))
    cfg=json.loads(cfg["data"]["backends.json"])["environments"]
    return cfg.get("base_digest") or cfg["base_image"]


def definition_key(p,base):
    # Mirror the envbuild.Definition.Key protocol, using bytes read through this lease.
    obj={"kind":"devcontainer","path":".devcontainer/devcontainer.json","context":".devcontainer",
         "content":p["content_sha256"],"dockerfile":p["dockerfile_sha256"],"base":base}
    return hashlib.sha256(json.dumps(obj,sort_keys=True).encode()).hexdigest()


def probe(iid,owner,user,workspace,timeout):
    doc=command(["kubectl","-n","agent-runtime","exec","-i","deploy/ar-agent-runtime-worker-program","--",
                 "python","-",iid,owner,user,workspace,str(timeout)],timeout+80,POD)
    return json.loads(doc.strip().splitlines()[-1])


def wait_ready(instance_id,tenant,user,workspace,timeout_s,output,poll_s=12):
    started=time.monotonic()
    out=Path(output)
    info={"instance_id":instance_id,"workspace":workspace,"tenant":tenant,"started_at":stamp(),
          "ready":False,"released":False,"status":"starting","observations":[]}
    def save():
        info.update(updated_at=stamp(),elapsed_seconds=round(time.monotonic()-started,3))
        out.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile("w",dir=out.parent,delete=False) as f:
            json.dump(info,f,indent=2);f.write("\n")
        os.replace(f.name,out)
    def note(event):
        if not info["observations"] or info["observations"][-1]["event"]!=event:
            info["observations"].append({"at":stamp(),"event":event});save()
    try:
        if not IID.fullmatch(instance_id) or not all(IDENT.fullmatch(v) for v in (tenant,user,workspace)):
            raise ValueError("invalid input identity")
        if not 10<=timeout_s<=3600 or not 1<=poll_s<=90: raise ValueError("invalid deadline")
        base=current_base_digest()
        expected_key=None
        deadline=started+timeout_s
        while time.monotonic()<deadline:
            if expected_key:
                row=registry_row(tenant,expected_key)
                note({"registry_state":row["state"]})
                if row["state"]=="building":
                    time.sleep(max(0,min(poll_s,deadline-time.monotonic())));continue
                if row["state"]!="ready":raise RuntimeError("native builder terminal state: "+row["state"])
            p=probe(instance_id,tenant,user,workspace,min(120,max(5,int(deadline-time.monotonic()))))
            note({"probe":p})
            info["released"]=p.get("released") is True
            if p.get("error") or not info["released"]:
                raise RuntimeError("probe failed: "+str(p.get("error","unreleased")))
            if p.get("writable") is not False or p.get("lease_in_workspace") is not True:
                raise RuntimeError("lease scope mismatch")
            if p.get("state") not in ("ready","building"):raise RuntimeError("unsupported resolution: "+str(p.get("state")))
            if p.get("definition_matches") is not True:raise RuntimeError("definition differs from requested issue")
            key=definition_key(p,base)
            if expected_key and key!=expected_key:raise RuntimeError("definition changed during prewarm")
            expected_key=key;info["definition_key"]=key
            if p["state"]!="ready":continue
            row=registry_row(tenant,key)
            if row['state']!='ready' or not SHA.fullmatch(row.get('digest','')) or not row['image'].endswith('@'+row['digest']):
                raise RuntimeError("no ready immutable image for observed definition")
            # K8s can report either the immutable manifest ref, its digest, or the local CRI config digest.
            cri=json.loads(command(["k3s","crictl","inspecti",row["image"]],25))["status"]
            if row["image"] not in cri.get("repoDigests",[]):raise RuntimeError("CRI has no registered immutable ref")
            actual=p["runtime_image_id"]
            if actual not in (row["image"],row["digest"],cri.get("id")):raise RuntimeError("lease image differs from registered image")
            info.update(ready=True,status="ready",registry_image=row["image"],runtime_image_id=actual,
                        registry_ready_at=row["ready_at"],native_build_ms=row["build_ms"],cri_config_id=cri.get("id"))
            return info
        raise TimeoutError("deadline before verified ready lease")
    except Exception as e:
        info.update(status="failed",error=str(e)[:220]);return info
    finally:save()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-id",required=True);parser.add_argument("--tenant",required=True)
    parser.add_argument("--user",required=True);parser.add_argument("--workspace",required=True)
    parser.add_argument("--timeout",type=int,default=900);parser.add_argument("--poll",type=int,default=12)
    parser.add_argument("--output",type=Path,required=True)
    a=parser.parse_args()
    j=wait_ready(a.instance_id,a.tenant,a.user,a.workspace,a.timeout,a.output,a.poll)
    print(json.dumps({k:j.get(k) for k in ("instance_id","ready","status","error","definition_key","registry_image","released")}))
    raise SystemExit(0 if j["ready"] else 1)
