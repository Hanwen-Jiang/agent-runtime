"""Prepare a verified quota-interrupted native run. Default and simulation issue no model calls."""
import argparse
from collections import Counter
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

DEPLOY=Path('/opt/evals/deployments/capability-resume-20261123')
RUNROOT=Path('/opt/evals/mx')
PROOF_SHA='870573ad8f98216258ffdea86a2d09736ae2f1a9e88f9f0c51280c091dc9d6b2'

def sha(data):return hashlib.sha256(data).hexdigest()
def load(path):return json.loads(path.read_bytes())
def require(value,message):
    if not value:raise ValueError(message)
def replace(path,raw):
    if path.exists() and path.read_bytes()==raw:return
    with tempfile.NamedTemporaryFile('wb',dir=path.parent,delete=False) as stream:
        stream.write(raw);stream.flush();os.fsync(stream.fileno())
        tmp=Path(stream.name)
    tmp.replace(path)
def encoded(doc):return (json.dumps(doc,sort_keys=True,ensure_ascii=False,indent=2)+'\n').encode()
def job(row):
    return ('/'.join((row['task_family'],row['role'],row['tier'])),row['eval'],row['candidate'],row['item'],row['repeat'])
def refused(row):
    rec=row.get('record') or {}
    return (bool((rec.get('model_call_failures') or {}).get('insufficient_user_quota')) or
            any(isinstance(err,str) and err.startswith('DeniedError: insufficient_user_quota:') for err in (row.get('error'),rec.get('error'))))
def run(name,apply=False,exercise=None):
    rid='capability-resume-20261123-'+name
    arch=DEPLOY/'paused-originals'/rid; out=RUNROOT/rid
    if exercise:
        dest=Path(exercise).resolve()
        require(dest!=RUNROOT and RUNROOT not in dest.parents and not (dest/rid).exists(),'simulation must target a new non-run location')
        dest.mkdir(parents=True,exist_ok=True);shutil.copytree(arch,dest/rid);out=dest/rid
    proofdir=DEPLOY/'quota-ledger-proof'
    proofpath=proofdir/'proof.json';proof=load(proofpath)
    require(sha(proofpath.read_bytes())==PROOF_SHA and proof['schema']=='matrix-quota-zero-settlement-v1' and proof['tenant']=='evals','unrecognised ledger proof')
    ledgerbytes=(proofdir/'ledger.jsonl').read_bytes()
    require(sha(ledgerbytes)==proof['ledger_sha256'],'ledger snapshot changed')
    ledger={x['reservation_id']:x for line in ledgerbytes.splitlines() if (x:=json.loads(line))}
    all_ids=[e['reservation_id'] for r in proof['runs'].values() for e in r['retryable'].values()]
    require(len(set(all_ids))==len(all_ids),'duplicate proof reservation')
    runproof=proof['runs'][rid]; decision=runproof['retryable']
    before=(arch/'results.jsonl').read_bytes()
    require(sha(before)==runproof['results_sha256'] and sha((arch/'run-lock.json').read_bytes())==runproof['run_lock_sha256'],'archived evidence differs')
    buckets=load(arch/'recovery-buckets-private.json')
    require(sha((arch/'recovery-buckets-private.json').read_bytes())==runproof['buckets_sha256'],'archived recovery key map differs')
    pause=load(arch/'provider-quota-pause.json')
    require(pause['run_id']==rid and pause['results_sha256']==sha(before) and pause['reason']=='insufficient_user_quota','pause is unbound')
    require((arch/'run-lock.json').read_bytes()==(out/'run-lock.json').read_bytes() and (arch/'plan.json').read_bytes()==(out/'plan.json').read_bytes(),'frozen run configuration changed')
    retained=[];reset={};recover=set()
    for line in before.splitlines(keepends=True):
        row=json.loads(line); key=job(row); slot=sha(json.dumps(key).encode())
        if slot not in decision:
            retained.append(line);continue
        e=decision[slot]
        require(list(key)==e['key'] and buckets[slot]=='quota_no_successful_call_recorded' and refused(row) and not row['calls'] and not row['cost_usd'],'cannot reopen model evidence or billed trial')
        r=ledger[e['reservation_id']]
        require(r['actual_usd_micros']==e['actual_usd_micros']==0 and r['task_id']==e['matched_task_id'] and r['node_id']==e['matched_node_id'] and r['model_profile_id']==e['matched_model_profile'],'settlement identity mismatch')
        name=slot+'.json'; b=load(arch/'item-budgets'/name)
        require(sha((arch/'item-budgets'/name).read_bytes())==e['budget_sha256'] and b.get('spent_usd_micros')==0,'previously paid trial')
        reset[name]=(e['budget_sha256'],encoded({**b,'pending':False}));recover.add(slot)
    require(recover==set(decision),'proof contains unknown slots')
    after=b''.join(retained)
    rel='provider-quota-pause.json';recpath=out/'resume-reconciliation.json'
    totals=Counter(buckets.values())
    rec={'schema':'matrix-native-quota-resume-v1','run_id':rid,'zero_settlement_proof_sha256':PROOF_SHA,
         'source_results_sha256':sha(before),'resumed_results_sha256':sha(after),'run_lock_sha256':runproof['run_lock_sha256'],
         'reopened_proven_zero':len(recover),'unattempted_no_observed_spend':totals.get('no_receipt_no_observed_spend',0),
         'retained_original_receipts':len(retained),'held_unreceipted':totals.get('no_receipt_preserve_reservation_or_spend',0)}
    with (out/'.matrix-owner.lock').open('a+b') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        if not (out/rel).exists():
            require(recpath.exists() and load(recpath)==rec,'run is not marked for this recovery');return rec
        require(load(out/rel)==pause,'active pause marker changed')
        require(sha((out/'results.jsonl').read_bytes()) in {sha(before),sha(after)},'live results changed')
        for file,(old,new) in reset.items():
            require(sha((out/'item-budgets'/file).read_bytes()) in {old,sha(new)},'live item budget changed')
        if apply or exercise:
            for file,(_,new) in reset.items():replace(out/'item-budgets'/file,new)
            replace(out/'results.jsonl',after)
            if (out/'scores.json').exists():
                require((arch/'scores.json').read_bytes()==(out/'scores.json').read_bytes(),'unexpected existing score')
                (out/'scores.json').rename(out/'scores.before-quota-recovery.json')
            replace(recpath,encoded(rec))
            (out/rel).rename(out/'provider-quota-pause.before-recovery.json')
    return rec

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',choices=['core','transform','judge-v2'])
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--confirmed-funding-restored',action='store_true',help='apply to original after funding was confirmed')
    mode.add_argument('--exercise-copy',type=Path,help='exercise transaction only on new private scratch copy, no provider calls')
    a=parser.parse_args();print(json.dumps(run(a.run,a.confirmed_funding_restored,a.exercise_copy),indent=2))
