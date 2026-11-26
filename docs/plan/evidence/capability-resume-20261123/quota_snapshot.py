"""Snapshot stopped native writes and add a verifiable, read-only dashboard pause marker."""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import fcntl,hashlib,json,os,shutil,sys,tempfile

ROOT=Path('/opt/evals/mx')
ARCHIVE=Path('/opt/evals/deployments/capability-resume-20261123/paused-originals')
RUNS=('core','transform','judge-v2')
def load(p):return json.loads(p.read_bytes())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def write(path,value):
    with tempfile.NamedTemporaryFile('w',dir=path.parent,delete=False) as stream:
        json.dump(value,stream,indent=2,ensure_ascii=False);stream.write('\n')
        tmp=Path(stream.name)
    tmp.replace(path)
def quota(r):
    rec=r.get('record') or {}
    if isinstance(rec.get('model_call_failures'),dict) and rec['model_call_failures'].get('insufficient_user_quota',0):
        return True
    return any(isinstance(s,str) and s.startswith('DeniedError: insufficient_user_quota:') for s in (r.get('error'),rec.get('error')))
def jobhash(key):return hashlib.sha256(json.dumps(key).encode()).hexdigest()
def key(r):
    return ('/'.join([r['task_family'],r['role'],r['tier']]),r['eval'],r['candidate'],r['item'],r['repeat'])

def snapshot(name):
    rid='capability-resume-20261123-'+name;p=ROOT/rid; archive=ARCHIVE/rid; archive.mkdir(exist_ok=True,parents=True,mode=0o700)
    with (p/'.matrix-owner.lock').open('a+b') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        files=sorted([f for f in p.glob('*') if f.is_file() and f.name not in ('.matrix-owner.lock','provider-quota-pause.json')])
        if not (archive/'run-lock.json').exists():
            for f in files:shutil.copy2(f,archive/f.name)
            shutil.copytree(p/'item-budgets',archive/'item-budgets')
        for f in files:assert sha(f)==sha(archive/f.name)
        rows=[json.loads(l) for l in (p/'results.jsonl').read_text().split('\n') if l.strip()]
        run_lock=load(p/'run-lock.json'); plan=run_lock['plan']
        jobkeys=set()
        for ev in run_lock['evaluations']:
            c=next(c for c in plan['cells'] if (c.get('task_family',plan.get('task_family','general.unclassified')),c['role'],c['tier'])==tuple(ev['cell'].split('/')))
            entry=next(e if isinstance(e,dict) else {'eval':e} for e in c['evals'] if (e['eval'] if isinstance(e,dict) else e)==ev['spec'])
            n=entry.get('n',c.get('n',plan['n']));rep=entry.get('r',c.get('r',plan['r']))
            jobkeys.update((ev['cell'],ev['name'],model,i,k) for model in c['candidates'] for i in ev['items'][:n] for k in range(rep))
        observed={key(r):r for r in rows};assert len(rows)==len(observed) and set(observed)<=jobkeys
        groups=Counter();bucket_keys={};first=None
        for k in sorted(jobkeys):
            r=observed.get(k); f=p/'item-budgets'/(jobhash(k)+'.json')
            b=load(f) if f.exists() else {}
            if r is None:
                if b.get('pending') or b.get('spent_usd_micros',0)>0:kind='no_receipt_preserve_reservation_or_spend'
                else:kind='no_receipt_no_observed_spend'
            elif quota(r):
                if first is None or r['at']<first:first=r['at']
                kind='quota_after_provider_observation' if (r['calls'] or r['cost_usd'] or b.get('spent_usd_micros',0)) else 'quota_no_successful_call_recorded'
            elif not r['error'] and not r['contaminated'] and r['pinned_calls'] and r['calls'].get(r['role']+':'+r['candidate'],0)>0:
                kind='clean_pinned_receipt'
            else:kind='other_existing_receipt'
            groups[kind]+=1;bucket_keys[jobhash(k)]=kind
        count=groups['quota_after_provider_observation']+groups['quota_no_successful_call_recorded']
        assert first and count
        marker={'schema':'capability-provider-pause-v1','run_id':rid,'reason':'insufficient_user_quota','active':True,
                'detected_at':datetime.now(timezone.utc).isoformat(),'results_sha256':sha(p/'results.jsonl'),
                'run_lock_sha256':sha(p/'run-lock.json'),'observed_rows':len(rows),'quota_rows':count,'first_quota_at':first,
                'evidence_source':'frozen native results and stopped writer'}
        markerfile=p/'provider-quota-pause.json'
        if markerfile.exists():
            prior=load(markerfile); assert dict(prior,detected_at=marker['detected_at'])==marker
            marker=prior
        write(markerfile,marker);write(archive/'provider-quota-pause.json',marker)
        summary={**marker,'planned':len(jobkeys),'groups':dict(groups),'known_cost_lower_bound_usd':round(sum(r['cost_usd'] for r in rows),6),
                 'rows_with_unknown_cost':sum(not r.get('cost_known',False) for r in rows),
                 'complete':False,'cost_note':'observed source charges only; outstanding amounts are unknown'}
        write(archive/'recovery-buckets-private.json',bucket_keys);write(archive/'summary.json',summary)
        return summary

if __name__=='__main__':
    print(json.dumps({n:snapshot(n) for n in RUNS},indent=2))
