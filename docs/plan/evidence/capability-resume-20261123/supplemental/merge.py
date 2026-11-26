"""Project merged logical trial outcomes and costs without editing either original ledger."""
import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import tempfile

from supplemental import DEPLOY, NAMES, NATIVE, PREFIX, STOP, SUPP, digest, is_quota, key, read, scheduled, sha

HASHES = {
    "core":"79659c292af3e42c20f4fdd61b96ed68518baa30112fec42fc7ec09d075f615b",
    "transform":"a4189297b6da8aba28e5831205f230d21e9f0d9e782ba6249bcc9185fba5b7ad",
    "judge-v2":"2740978d5ae2481a981b099215def1fc9b88a90b3bbfea4632965a6ce2a5155c",
}


def read_ledger(path):
    if not path.exists():
        return {}, hashlib.sha256(b"").hexdigest(), None
    with path.open("rb") as f:
        stat = os.fstat(f.fileno())
        data=f.read(stat.st_size)
        after=os.fstat(f.fileno())
        if stat.st_size!=len(data) or (stat.st_size,stat.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or (data and not data.endswith(b"\n")):
            raise BlockingIOError("Ledger changing; use next observation")
    rows = {}
    for line in data.split(b"\n"):
        if not line:
            continue
        row=json.loads(line)
        k=digest(key(row))
        if k in rows:
            raise ValueError("Duplicate receipt")
        rows[k]=row
    return rows,hashlib.sha256(data).hexdigest(),[stat.st_size,stat.st_mtime_ns]


def publish(path, doc, public=True):
    data=(json.dumps(doc,sort_keys=True,ensure_ascii=False,separators=(",",":"),allow_nan=False)+"\n").encode()
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:
        f.write(data)
        os.fchmod(f.fileno(),0o644 if public else 0o600)
        f.flush()
        os.fsync(f.fileno())
    Path(f.name).replace(path)
    return hashlib.sha256(data).hexdigest()


def category(row):
    if is_quota(row):
        return "provider_quota_refusal"
    issues=(row.get("record") or {}).get("model_call_failures") or {}
    text=(row.get("error") or "")+" "+str(issues)
    if "model_usage_pending" in text:
        return "provider_usage_pending"
    if "bad_structured_output" in text:
        return "invalid_structured_response"
    if "model_call_budget_exceeded" in text or "item_usd_limit" in text:
        return "per_item_budget_or_reservation"
    if row.get("error"):
        return "other_adapter_or_service_error"
    wanted=row["role"]+":"+row["candidate"]
    if row.get("contaminated") or row.get("missed_calls"):
        return "pin_contaminated_or_call_failed"
    if not row.get("pinned_calls") or not row.get("calls",{}).get(wanted,0):
        return "no_confirmed_pinned_call"
    return "scored_correct" if row.get("score",0)>=1 else "scored_unsuccessful_or_partial"


def merge(name, final=False):
    rid=PREFIX+name
    native=NATIVE/rid
    supp=SUPP/rid
    mapping_file=supp/"attempts-private.json"
    assert sha(mapping_file)==HASHES[name]
    manifest=read(mapping_file)
    lock=read(native/"run-lock.json")
    lock_sha=sha(native/"run-lock.json")
    assert lock_sha==manifest["parent_lock_sha256"]==sha(supp/"run-lock.json")
    assert manifest["parent_run"]==rid and read(native/"plan.json")==lock["plan"]==read(supp/"plan.json")
    expected=scheduled(lock)
    chosen=manifest["slots"]
    assert set(chosen) <= expected.keys()
    base,raw_sha,raw_stat=read_ledger(native/"results.jsonl")
    extra,extra_sha,extra_stat=read_ledger(supp/"results.jsonl")
    if set(base)-expected.keys() or set(extra)-chosen.keys():
        raise ValueError("Receipt outside planned cohort")
    plan_hash=hashlib.sha256(json.dumps(lock['plan'],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    experiment_hash=hashlib.sha256(json.dumps(lock,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    versions={(e['cell'],e['name']):str(e['version']) for e in lock['evaluations']}
    for group in (base,extra):
        for k,row in group.items():
            identity=key(row)
            if identity!=expected[k] or row['plan']!=rid or row['plan_sha256']!=plan_hash or row['experiment_sha256']!=experiment_hash or not row['deployment_frozen'] or versions[identity[:2]]!=str(row['version']):
                raise ValueError("Receipt provenance differs from original evaluator")
    for k in chosen.keys()&base.keys():
        if not is_quota(base[k]):
            raise ValueError("Overlapping non-quota native receipt")
    expcounts=Counter((v[0],v[2]) for v in expected.values())
    counts={k:{'column':'.'.join(k[0].rsplit('/',1)),'model':k[1],'expected':num,'runs':0,'clean':0,'quota':0,'terminal':0} for k,num in expcounts.items()}
    disposition=Counter()
    selected={}
    picked=Counter()
    # The original attempt's eligible expenses remain attributable to the logical slot.
    native_known=sum(Decimal(str(r.get('cost_usd') or 0)) for r in base.values())
    extra_known=sum(Decimal(str(r.get('cost_usd') or 0)) for r in extra.values())
    interrupted=0
    present_pending=0
    for k in chosen:
        if k not in base:
            budget=read(STOP/rid/'item-budgets'/(k+'.json'))
            interrupted += int(budget.get('spent_usd_micros',0))
            present_pending += bool(budget.get('pending'))
    for k,(cell,ev,model,i,repeat) in expected.items():
        if k in extra:
            row=extra[k];source="supplemental"; prior=base.get(k)
            r=dict(row)
            old_usd=0
            if prior:
                old_usd=prior.get("cost_usd") or 0; known=prior.get("cost_known") is True
            else:
                old=read(STOP/rid/'item-budgets'/(k+'.json'))
                old_usd=old.get("spent_usd_micros",0)/1e6; known=not old.get("pending")
            r["cost_usd"]=round(float(row.get("cost_usd") or 0)+old_usd,6)
            r["cost_known"]=known and row.get("cost_known") is True
        elif k in base:
            row=base[k];r=dict(row);source="native"
        else:
            continue
        reason=category(row)
        clean=reason in ("scored_correct","scored_unsuccessful_or_partial")
        # Keep frozen native evaluator scores. For an observation with zero actual
        # pinned model outputs, no score can be attributed to the requested model.
        if not clean and not r.get('contaminated'):
            r['contaminated']=True
        selected[k]=r
        c=counts[cell,model];c['runs']+=1;c['clean']+=int(clean);c['quota']+=int(is_quota(row));c['terminal']+=int(not is_quota(row))
        disposition[reason]+=1;picked[source]+=1
    pause=None
    for directory in (native,supp):
        path=directory/"provider-quota-pause.json"
        if path.exists():
            m=read(path)
            if m.get("reason")=="insufficient_user_quota" and m.get("active") is True and m.get("run_lock_sha256")==lock_sha:
                pause={'reason':m['reason'],'detected_at':m.get('detected_at'),'marker_sha256':sha(path),'source':"native" if directory==native else "supplemental"}
                break
    complete=sum(c['terminal'] for c in counts.values())==len(expected) and not pause
    known=native_known+extra_known+Decimal(interrupted)/Decimal(1_000_000)
    doc={"schema":"matrix-supplement-coverage-v1","run_id":rid,"lock_sha256":lock_sha,"manifest_sha256":HASHES[name],
         "native_ledger_sha256":raw_sha,"native_signature":raw_stat,"supplemental_ledger_sha256":extra_sha,"supplemental_signature":extra_stat,
         "checked_at":datetime.now(timezone.utc).isoformat(),"counts":[counts[k] for k in sorted(counts)],
         "expected":len(expected), "observed":len(selected), "terminal_dispositions":sum(c['terminal'] for c in counts.values()),
         "clean":sum(c['clean'] for c in counts.values()),"quota":disposition["provider_quota_refusal"],"complete":complete and final,
         "scores_sha256":None,"provider_pause":pause,"native_receipts":len(base),"supplemental_receipts":len(extra),
         "selected_sources":dict(picked),"counts_by_disposition":dict(disposition),
         "native_recorded_usd":float(native_known),"supplemental_recorded_usd":float(extra_known),
         "interrupted_no_receipt_known_usd":interrupted/1e6,"total_known_usd_lower_bound":float(known),
         "source_rows_with_unknown_cost":sum(r.get("cost_known") is not True for r in list(base.values())+list(extra.values())),
         "interrupted_no_receipt_pending":present_pending,"registered_supplemental":len(chosen)}
    if final:
        if not complete:
            raise ValueError('Missing proper disposition for at least one original slot')
        from agentruntime_learning.capability import matrix
        scorer=matrix.score_results(matrix.parse_plan(lock["plan"]),list(selected.values()))
        scorer['scored_at']=max(str(r['at']) for r in selected.values())
        scorer['aggregation']={k:doc[k] for k in ('schema','run_id','lock_sha256','manifest_sha256','native_ledger_sha256','supplemental_ledger_sha256','native_receipts','supplemental_receipts','selected_sources')}
        assert sum(c['runs'] for cell in scorer['cells'] for c in cell['candidates'])==len(expected)
        doc['scores_sha256']=publish(native/'supplemental-scores.json',scorer)
        detail=[{'slot_sha256':k,'used':'supplemental' if k in extra else 'native','attempt_id':chosen[k]['proposed_attempt_id'] if k in extra else None,
                  'outcome':category(extra.get(k,base.get(k))),'score':row['score'],'cost_known':row['cost_known'],
                  'observed_known_total_usd':row['cost_usd']} for k,row in selected.items()]
        publish(supp/'final-selection-private.json',detail,public=False)
    publish(native/'public-supplemental-coverage.json',doc)
    return {k:doc[k] for k in ('run_id','expected','observed','terminal_dispositions','clean','quota','supplemental_receipts','total_known_usd_lower_bound','complete','scores_sha256')}


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('group',choices=NAMES);p.add_argument('--final',action='store_true');args=p.parse_args()
    print(json.dumps(merge(args.group,args.final)))
