"""Build private A/B services using the deployed Runtime images and an exact two-file overlay.
Run inside the authorized evaluation machine. Never store source deployment specs or credentials.
"""
import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NS = "agent-runtime"
PREFIX = "mini50-1121"
MARK = {"agentruntime.io/experiment": PREFIX}
PROFILE = "a6api/gpt-6.2-sol@2026-11"
TIER = "strong@medium"
WORKER_PATH = "/app/services/py-worker/src/agentruntime_worker"
FILES = ["reviewer/rules.py", "workflows/verification.py"]
POOLS = ["task","workspace","decision","program","model"]
TEMPS = {arm:f"ar-mini50-20261121-{arm}" for arm in ("a","b")}
COMP = "app.kubernetes.io/component"
INST = "app.kubernetes.io/instance"
NAME = "app.kubernetes.io/name"


def run(cmd, data=None, *, check=True):
    proc = subprocess.run(cmd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode:
        # Never include an object body; API dry-run/validation responses can quote env settings.
        err = proc.stderr.decode(errors="replace").splitlines()
        safe = err[0][:220] if err and len(err[0]) < 250 and not any(v in err[0] for v in ["{","Bearer","postgres://"]) else "command failed"
        raise RuntimeError(f"{cmd[0]} {cmd[1:4]}: {proc.returncode}: {safe}")
    return proc


def kub(*args):
    return run(["kubectl","-n",NS,*args]).stdout


def get(kind, name):
    return json.loads(kub("get",kind,name,"-o","json"))


def labels(extra=None):
    return {**MARK,**(extra or {})}


def obj(kind, name, **spec):
    return {"apiVersion":"networking.k8s.io/v1" if kind=="NetworkPolicy" else "apps/v1" if kind=="Deployment" else "v1",
            "kind":kind,"metadata":{"name":name,"namespace":NS,"labels":labels()},**spec}


def service(name, arm, component, ports):
    sel=labels({"agentruntime.io/arm":arm,"agentruntime.io/role":component})
    return obj("Service",name,spec={"type":"ClusterIP","selector":sel,
        "ports":[{"name":f"p{p}","port":p,"targetPort":p,"protocol":"TCP"} for p in ports]})


def network(name, arm, role, ports):
    sel=labels({"agentruntime.io/arm":arm,"agentruntime.io/role":role})
    peers=labels({"agentruntime.io/role":"worker"})
    if arm!="shared": peers["agentruntime.io/arm"]=arm
    rules=[{"from":[{"podSelector":{"matchLabels":peers}}],"ports":[{"protocol":"TCP","port":p} for p in ports]}]
    return obj("NetworkPolicy",name,spec={"podSelector":{"matchLabels":sel},"policyTypes":["Ingress"],"ingress":rules})


def setenv(c,k,v):
    c["env"]=[e for e in c.get("env",[]) if e["name"]!=k]
    if v is not None:c["env"].append({"name":k,"value":v})


def novol(p,c,keys):
    p["volumes"]=[v for v in p.get("volumes",[]) if v["name"] not in keys]
    c["volumeMounts"]=[v for v in c.get("volumeMounts",[]) if v["name"] not in keys]


def clone(original, name, arm, role, pool=None):
    template=copy.deepcopy(original["spec"]["template"])
    owned=labels({"agentruntime.io/arm":arm,"agentruntime.io/role":role})
    if pool: owned["agentruntime.io/arm-pool"]=pool
    lab=template["metadata"]["labels"]
    if pool:
        # Keeps the normal worker identity accepted by the sandbox service.
        # A different pool label excludes these pods from all production pool selectors.
        lab["agentruntime.io/pool"]=f"{PREFIX}-{arm}-{pool}"
    else:lab[INST]=PREFIX
    lab.update(owned)
    template["metadata"]={"labels":lab}
    p=template["spec"]
    assert len(p["containers"])==1 and not p.get("initContainers")
    assert p.get("automountServiceAccountToken") is False
    c=p["containers"][0]
    c["imagePullPolicy"]="IfNotPresent"
    # The isolated catalog is only usable when the tenant policy accepts the
    # same profile. Keep the policy in the experiment-owned ConfigMap so the
    # shared production allowlist is never broadened.
    for v in p.get("volumes", []):
        if v.get("name") == "tenant-policies" and "configMap" in v:
            v["configMap"]["name"] = PREFIX + "-policies"
    setenv(c,"OTEL_SERVICE_VERSION",f"{PREFIX}-{arm}-{role}")
    setenv(c,"AR_SECRETS_SEED_FILE",None)
    if role in ("worker","api"):
        novol(p,c,{"secrets"})
        setenv(c,"AR_TEMPORAL_NAMESPACE",TEMPS[arm])
    if role=="worker":
        # No shared signing key, extra judge or memory learning across experimental arms.
        for k in ("AR_DEV_SIGNING_KEY","AR_MEMORY_ADDRESS","AR_JUDGE","AR_JUDGE_KEY_FILE"):setenv(c,k,None)
        novol(p,c,{"judge-key"})
        setenv(c,"AR_MODEL_GATEWAY_ADDRESS",PREFIX+"-gw:8091")
        setenv(c,"AR_TASK_SERVICE_INTERNAL_URL",f"http://{PREFIX}-{arm}-api:8097")
        setenv(c,"AR_MODEL_ROUTING_OVERRIDES_FILE","/etc/agentruntime/mini50-routing/overrides.json")
        if arm=="b":
            p["volumes"].append({"name":"reviewer-change","configMap":{"name":PREFIX+"-candidate"}})
            for f in FILES:
                basename=f.split("/")[-1]
                c["volumeMounts"].append({"name":"reviewer-change","mountPath":WORKER_PATH+"/"+f,"subPath":basename,"readOnly":True})
        p["volumes"].append({"name":"route-lock","configMap":{"name":PREFIX+"-router"}})
        c["volumeMounts"].append({"name":"route-lock","mountPath":"/etc/agentruntime/mini50-routing","readOnly":True})
    elif role == "gateway":
        # The isolated gateway must consume the isolated catalog. The shared
        # published catalog is never mutated.
        for v in p.get("volumes", []):
            if v.get("name") == "model-profiles" and "configMap" in v:
                v["configMap"]["name"] = PREFIX + "-profiles"
        p["volumes"].append({"name":"route-lock","configMap":{"name":PREFIX+"-router"}})
        c["volumeMounts"].append({"name":"route-lock","mountPath":"/etc/agentruntime/mini50-routing","readOnly":True})
        setenv(c,"AR_MODEL_ROUTING_OVERRIDES_FILE","/etc/agentruntime/mini50-routing/overrides.json")
        p["volumes"].append({"name":"route-lock","configMap":{"name":PREFIX+"-router"}})
        c["volumeMounts"].append({"name":"route-lock","mountPath":"/etc/agentruntime/mini50-routing","readOnly":True})
        setenv(c,"AR_MODEL_ROUTING_OVERRIDES_FILE","/etc/agentruntime/mini50-routing/overrides.json")
    elif role=="api":
        setenv(c,"AR_INGRESS_DEV_TOKENS","refuse")
        setenv(c,"AR_DEV_SIGNING_KEY",None)
        setenv(c,"AR_PUBLIC_BASE_URL",f"http://{PREFIX}-{arm}-api:8080")
        vol=next(v for v in p["volumes"] if v["name"]=="ingress-issuers")
        vol["configMap"]["name"]=PREFIX+"-issuers"
    else:
        p["volumes"].append({"name":"route-lock","configMap":{"name":PREFIX+"-router"}})
        c["volumeMounts"].append({"name":"route-lock","mountPath":"/etc/agentruntime/mini50-routing","readOnly":True})
        setenv(c,"AR_MODEL_ROUTING_OVERRIDES_FILE","/etc/agentruntime/mini50-routing/overrides.json")
    return obj("Deployment",name,spec={"replicas":1,"strategy":{"type":"Recreate"},
        "selector":{"matchLabels":owned},"template":template})


def documents():
    api=get("deployment","ar-agent-runtime-task-service")
    svc_list=json.loads(kub("get","service","-o","json"))["items"]
    base=get("deployment","ar-agent-runtime-model-gateway")
    pools={p:get("deployment","ar-agent-runtime-worker-"+p) for p in POOLS}
    for d in (api,base,*pools.values()):
        if d["status"].get("readyReplicas",0)<1:raise ValueError("base workload is not ready")
    # This ConfigMap has public JWT verification keys only. Limit the accepted tenants
    # and issuer; no private signer/issuer material is copied or read.
    issuer_ref=next(v["configMap"]["name"] for v in api["spec"]["template"]["spec"]["volumes"] if v["name"]=="ingress-issuers")
    issuers=json.loads(get("cm",issuer_ref)["data"]["issuers.json"])
    selected=next(x for x in issuers["issuers"] if x["iss"]=="agentruntime-ops")
    assert {"evals","swebench"}<=set(selected["tenants"])
    issuers["issuers"]=[{**selected,"tenants":["evals","swebench"]}]
    cfg=json.loads((ROOT/"freeze.json").read_text())
    code={f.split("/")[-1]:(ROOT/"candidate"/f.split("/")[-1]).read_text() for f in FILES}
    assert all(hashlib.sha256(code[f.split("/")[-1]].encode()).hexdigest()==cfg["candidate_files"][f] for f in FILES)
    published=next(v for v in get("cm","agent-runtime-model-profiles-published20261107")["data"].values()
                   if '"profiles"' in v)
    catalog=json.loads(published)
    old=next(p for p in catalog["profiles"] if p.get("id")=="a6api/gpt-6.1-sol@2026-11")
    latest=json.loads(json.dumps(old))
    latest["id"],latest["model"] = PROFILE,"gpt-6.2-sol"
    latest["freshness"]={"price_at":"2026-11-21"}
    catalog["profiles"]=[p for p in catalog["profiles"] if p.get("id")!=PROFILE]+[latest]
    revisions=catalog.get("registry_revisions", {})
    if "a6api/gpt-6.1-sol@2026-11" not in revisions:
        raise ValueError("base profile registry revision missing")
    revisions[PROFILE]=json.loads(json.dumps(revisions["a6api/gpt-6.1-sol@2026-11"]))
    canonical=json.dumps(latest,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()
    revisions[PROFILE]["sha256"]=hashlib.sha256(canonical).hexdigest()
    catalog["registry_revisions"]=revisions
    policy_ref=next(v["configMap"]["name"] for v in base["spec"]["template"]["spec"]["volumes"]
                    if v["name"] == "tenant-policies")
    policy_doc=json.loads(get("cm",policy_ref)["data"]["tenant-policies.json"])
    swebench=policy_doc.setdefault("tenants", {}).setdefault("swebench", {})
    allowlist=swebench.setdefault("model_allowlist", [])
    if PROFILE not in allowlist:
        allowlist.append(PROFILE)
    routes={r:{"profile":PROFILE,"tier":TIER} for r in ("fetch","transform","plan","route","judge","aggregate","act","gate","deliver","summary","probe")}
    # No embedding model is used as a substitute for a generation role.
    routes["embedding"]={"profile":"a6api/text-embedding-3-large@2026-10","tier":"embed@default"}
    routing={"tenants":{n:routes for n in ("evals","swebench")}}
    result=[obj("ConfigMap",PREFIX+"-profiles",immutable=True,data={"model-profiles.json":json.dumps(catalog,sort_keys=True)}),
            obj("ConfigMap",PREFIX+"-policies",immutable=True,data={"tenant-policies.json":json.dumps(policy_doc,sort_keys=True)}),
            obj("ConfigMap",PREFIX+"-router",immutable=True,data={"overrides.json":json.dumps(routing,sort_keys=True)}),
            obj("ConfigMap",PREFIX+"-issuers",immutable=True,data={"issuers.json":json.dumps(issuers,sort_keys=True)}),
            obj("ConfigMap",PREFIX+"-candidate",immutable=True,data=code),
            network(PREFIX+"-gw","shared","gateway",[8091]),service(PREFIX+"-gw","shared","gateway",[8091]),
            clone(base,PREFIX+"-gw","shared","gateway")]
    for arm in ("a","b"):
        name=PREFIX+f"-{arm}-api"
        result.extend([network(name,arm,"api",[8080,8097]),service(name,arm,"api",[8080,8097]),clone(api,name,arm,"api")])
        for pool in POOLS:
            result.append(clone(pools[pool],PREFIX+f"-{arm}-{pool}",arm,"worker",pool))
    # Refuse a collision, including any service not created by this script.
    for d in result:
        if d["kind"]!="Deployment":continue
        lab=d["spec"]["template"]["metadata"]["labels"]
        for existing in svc_list:
            if existing["metadata"].get("labels",{}).get("agentruntime.io/experiment")==PREFIX:continue
            sel=existing["spec"].get("selector",{})
            if sel and all(lab.get(k)==v for k,v in sel.items()):
                raise RuntimeError("experiment pod matched existing service: "+existing["metadata"]["name"])
    return result


def temporal(*args,check=True):
    return run(["kubectl","-n","data","exec","deploy/temporal-admintools","--","temporal","operator","namespace",*args,"--address","temporal-frontend:7233"],check=check)


def apply(docs,dry):
    for d in docs:
        # A unique, task-owned resource may be reapplied, not an arbitrary name collision.
        k=d["kind"];n=d["metadata"]["name"]
        exists=kub("get",k,n,"--ignore-not-found","-o","json")
        if exists and json.loads(exists)["metadata"]["labels"].get("agentruntime.io/experiment")!=PREFIX:
            raise RuntimeError("object name belongs to a different owner")
        cmd=["kubectl","-n",NS,"apply","-f","-"]
        if dry:cmd.append("--dry-run=server")
        run(cmd,json.dumps(d).encode())
    print(f"{'verified' if dry else 'applied'} {len(docs)} scoped resources",flush=True)


if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("command",choices=("check","apply","status","delete"));a=p.parse_args()
    if a.command=="delete":
        # Preserve active submissions; remove only this installation after evidence export.
        for ns in TEMPS.values():
            executions = run(["kubectl","-n","data","exec","deploy/temporal-admintools","--","temporal","workflow","list",
                              "--namespace",ns,"--address","temporal-frontend:7233","--output","json","--query","ExecutionStatus='Running'","--limit","1"])
            if json.loads(executions.stdout):
                raise SystemExit(f"refusing cleanup: {ns} still has a running workflow")
        pidfile=ROOT/"meter.pid"
        if pidfile.exists():
            pid=int(pidfile.read_text().strip());cmdline=Path(f"/proc/{pid}/cmdline")
            if cmdline.exists() and Path(f"/proc/{pid}/cwd").resolve()==ROOT and b"meter_watch.py" in cmdline.read_bytes().split(b"\0"):
                os.kill(pid,15)
        print(kub("delete","deployment,service,networkpolicy,configmap","-l","agentruntime.io/experiment="+PREFIX,"--wait=false").decode())
    elif a.command=="status":
        print(kub("get","deployments,pods,services","-l","agentruntime.io/experiment="+PREFIX,"-o","wide").decode())
    else:
        ds=documents()
        apply(ds,True)
        if a.command=="apply":
            for ns in TEMPS.values():
                if temporal("describe","--namespace",ns,check=False).returncode:
                    temporal("create","--namespace",ns,"--retention","7d","--description","Scoped SWE Mini50 experiment 2026-11-21")
            apply(ds,False)
