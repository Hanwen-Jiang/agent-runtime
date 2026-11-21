"""Bind selected task IDs to tenant-RLS ledger, isolated gateway meters and Temporal execution.
Run on cloudnium in the frozen arms directory after collection; never infer a served tier from configuration.
"""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
ROOT=Path(__file__).resolve().parent
PROFILE="a6api/gpt-6.2-sol@2026-11"
TIER="strong@medium"
def digest(obj):return hashlib.sha256(json.dumps(obj,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()
def tally(rows,keys):
    return collections.Counter(tuple(row.get(k) for k in keys) for row in rows)
def raw(cmd, obj=None):
    return subprocess.check_output(cmd, input=json.dumps(obj).encode() if obj is not None else None)
def db_rows(arm,tenant,ids):
    if not ids:return []
    source=(ROOT/"ledger_rows.py").read_text()
    return json.loads(raw(["kubectl","-n","agent-runtime","exec","-i","deploy/mini50-1121-"+arm+"-task","--","python","-c",source],
                          {"tenant":tenant,"task_ids":ids}))
def flows(arm):
    namespace=f"ar-mini50-20261121-{arm}"
    cmd=["kubectl","-n","data","exec","deploy/temporal-admintools","--","temporal","workflow","list","--namespace",namespace,"--address","temporal-frontend:7233","--output","json","--query","WorkflowType='TaskWorkflow'","--limit","10000"]
    rows=json.loads(raw(cmd))
    return collections.Counter(x["execution"]["workflowId"] for x in rows if isinstance(x,dict) and isinstance(x.get("execution"),dict))
def atomic(path,value):
    tmp=path.with_name(path.name+".writing");tmp.write_text(json.dumps(value,indent=2,sort_keys=True,ensure_ascii=False)+"\n");tmp.replace(path)
def main():
    a=argparse.ArgumentParser();a.add_argument("run_dir",type=Path);a.add_argument("arm",choices=("a","b"));a.add_argument("--tenant",default="swebench",choices=("swebench","evals"));a.add_argument("--meter-file",type=Path,default=ROOT/"meter.jsonl");args=a.parse_args()
    out=args.run_dir;freeze=json.loads((ROOT/"freeze.json").read_text());man=json.loads((out/"run.json").read_text());ids=man["instance_ids"];assert len(ids)==len(set(ids)) and 0<len(ids)<=100
    registry=json.loads((ROOT/"pin-registry.json").read_text())[args.arm]
    if (registry["model_profile_id"],registry["tier"])!=(PROFILE,TIER):raise ValueError("unexpected frozen model")
    records={}
    for f in sorted((out/"records").glob("*.json")):
        r=json.loads(f.read_text());i=r["instance_id"]
        if i not in ids or i in records:raise ValueError("unexpected or duplicate case record")
        records[i]=r
    wanted=[r["task_id"] for r in records.values() if r.get("task_id")]
    if len(set(wanted))!=len(wanted):raise ValueError("a Task ID was reused for multiple cases")
    ledger=collections.defaultdict(list);meters=collections.defaultdict(list)
    for r in db_rows(args.arm,args.tenant,wanted):ledger[r["task_id"]].append(r)
    for l in args.meter_file.open():
        if not l.strip():continue
        r=json.loads(l)
        if r["tenant_id"]==args.tenant and r["task_id"] in wanted:meters[r["task_id"]].append(r)
    workflow_ids=flows(args.arm)
    settings={"generation_profile":PROFILE,"tier":TIER,"thinking_budget":8192,"memory_remote":False,"external_jev":False,
              "environment":man["settings"].get("environment"),"hints":man["settings"].get("hints"),"limits":man["limits"],
              "selection":sorted(ids),"tenant":args.tenant,"provider":"a6api","registry_document_sha256":registry["registry_document_sha256"],"registry_revision":registry["registry_revision"]}
    report={"schema":"swe-isolated-model-audit-v1","run_id":man["run_id"],"model_profile":PROFILE,"tier":TIER,"settings_digest":digest(settings),
            "runtime_revision":"git-tree:"+freeze["baseline_tree" if args.arm=="a" else "candidate_tree"],"evidence":{"Temporal":f"ar-mini50-20261121-{args.arm}",
            "database":"tenant-bound read-only model_usage","gateway":"dedicated per-call metering","scope":"observed gateway-resolved profile and tier, not an upstream model-weights hash"},"items":{}}
    for i in ids:
        rec=records.get(i,{});t=rec.get("task_id");db=ledger[t];ms=meters[t]
        units=[r for r in db if r["method"]!="embed"];io=[r for r in ms if r["method"]!="embed"]
        errors=[];nflows=workflow_ids[t] if t else 0
        if not t:errors.append("no_submitted_task")
        if nflows!=1:errors.append("unexpected_workflow_attempt_count")
        if not units:errors.append("no_generation_ledger")
        if not io:errors.append("no_gateway_meter")
        if any(r.get("actual_usd_micros") is None for r in db):errors.append("unsettled_ledger")
        if tally(units,["node_id","model_profile_id","method","actual_usd_micros"])!=tally(io,["node_id","model_profile_id","method","cost_usd_micros"]):errors.append("unmatched_ledger_meter")
        if any(r.get("model_profile_id")!=PROFILE for r in units):errors.append("unexpected_profile")
        if any(r.get("tier")!=TIER for r in io):errors.append("unexpected_tier")
        if any(any(r.get(k)!=registry[k] for k in ("registry_document_sha256","registry_revision","registry_scope")) for r in units):errors.append("unknown_or_changed_registry_revision")
        if any(r.get("usage_missing") for r in ms):errors.append("unknown_usage")
        fcost=sum((r.get("actual_usd_micros") or 0) for r in db)
        report["items"][i]={"task_id":t,"generation_attempts":nflows,"model_calls":len(units),
            "actual_profiles":dict(collections.Counter(r["model_profile_id"] for r in units)), "actual_tiers":dict(collections.Counter(r["tier"] for r in io)),
            "roles":dict(collections.Counter(r.get("role","") for r in io)), "registry_digests":sorted({r["registry_document_sha256"] for r in units if r.get("registry_document_sha256")}),
            "gateway_pod_uids":sorted({r["gateway_pod_uid"] for r in io}),"input_tokens":sum(r.get("input_tokens",0) for r in io),
            "output_tokens":sum(r.get("output_tokens",0) for r in io),"settled_cost_usd":round(fcost/1e6,6),"embedding_calls":len(db)-len(units),
            "audit_complete":not errors,"issues":errors,"official_environment_verified":rec.get("environment_used") is True}
    report["audit_complete"]=all(r["audit_complete"] for r in report["items"].values())
    atomic(out/"model-audit.json",report)
    print(json.dumps({"run_id":man["run_id"],"tasks":len(wanted),"valid_model_audits":sum(v["audit_complete"] for v in report["items"].values()),"items":len(ids),"path":str(out/"model-audit.json")}))
if __name__=="__main__":main()
