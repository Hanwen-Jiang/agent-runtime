"""Read stopped receipts and a read-only PG snapshot. Never schedule or alter an experiment."""
import argparse
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess

ROOT = Path('/opt/evals/deployments/capability-resume-20261123')
ARCHIVE = ROOT/'paused-originals'
DIRECT = {'bfcl-v4','longbench-v2','judgebench','rewardbench2'}
RUNS = ['capability-resume-20261123-' + s for s in ('core','transform','judge-v2')]
MIN_MARGIN_MS = 1000
SCHEMA = 'matrix-quota-zero-settlement-v1'

def read(path):
    return json.loads(path.read_bytes())

def digest(path):
    return sha256(path.read_bytes()).hexdigest()

def to_key(row):
    return ('/'.join([row['task_family'],row['role'],row['tier']]), row['eval'],row['candidate'],row['item'],row['repeat'])

def hkey(key):
    return sha256(json.dumps(key).encode()).hexdigest()

def write(path, payload):
    with path.open('x',encoding='utf-8') as f:
        os.fchmod(f.fileno(),0o600)
        f.write(payload)

def iso(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat(timespec='milliseconds')

def timestamp(value):
    return int(datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()*1000)

def jobs(lock):
    plan = lock['plan']
    cells = {'/'.join((c.get('task_family',plan.get('task_family','general.unclassified')),c['role'],c['tier'])):c for c in plan['cells']}
    for ev in lock['evaluations']:
        cell=cells[ev['cell']]
        entry=next(({'eval':x} if isinstance(x,str) else x) for x in cell['evals'] if (x if isinstance(x,str) else x['eval'])==ev['spec'])
        n=entry.get('n',cell.get('n',plan['n']))
        r=entry.get('r',cell.get('r',plan['r']))
        for model in cell['candidates']:
            for item in ev['items'][:n]:
                for repeat in range(r):
                    yield (ev['cell'],ev['name'],model,item,repeat)

@lru_cache(None)
def fixture(eval_name,item):
    base=ROOT/'src/services/py-learning/evalsets'
    if eval_name=='s11':
        data=read(base/'task-loop/context_samples'/(item+'.json'))
        return data['task_state']['task_id'],data['node']['id']+'#summary'
    if eval_name in ('s09','s09-route'):
        if item.startswith('adapted:'):
            group, identity=item[len('adapted:'):].split('/')
            folder=base/'public-adapted-drafts'/group/'task_cases'
        else:
            folder=base/'task-loop/task_cases'; identity=item
        data=read(folder/(identity+'.json'))['planner']['input']
        return (data.get('task_state') or {}).get('task_id') or ('tsk_01J'+'0'*22+'E'), 'triage'
    raise ValueError(eval_name)

def possible_owners(key):
    """Request namespaces that can compete with an identified provider reservation."""
    _,e,m,item,_=key
    if e in DIRECT:
        return [(e+':'+item, m)]
    if e in ('s09','s09-route','s11'):
        task,_=fixture(e,item)
        # The planning evaluator can call an unpinned triage assistant before planning.
        return [(task,'*' if e=='s09' else m)]
    return []

def expected_identity(row):
    e,m,i=row['eval'],row['candidate'],row['item']
    rec=row['record']
    if row.get('pinned_calls')!=1 or row.get('missed_calls')!=1:
        return None
    if e in DIRECT:
        calls=rec.get('calls') or []
        if len(calls)!=1:
            return None
        call=calls[0]
        if call.get('role')!=row['role'] or call.get('error')!='DeniedError' or call.get('cost_usd')!=0 or not call.get('node'):
            return None
        return (e+':'+i,call['node'],m)
    if e in ('s09-route','s11'):
        task,node=fixture(e,i)
        return (task,node,m)
    # Multiple local plan retries, route fallback and no target-provider observation coexist.
    return None

def typed(row):
    rec=row.get('record') or {}
    if (rec.get('model_call_failures') or {}).get('insufficient_user_quota',0)>0:
        return True
    return any(isinstance(s,str) and s.startswith('DeniedError: insufficient_user_quota:') for s in (row.get('error'),rec.get('error')))

def fetch_ledger(start,end,replay=None,expected_digest=None):
    assert isinstance(start,int) and isinstance(end,int) and 0<end-start<24*3600*1000
    query=f"""begin transaction isolation level repeatable read read only;
    select json_build_object('snapshot_at',clock_timestamp(),'database',current_database(),
           'transaction_snapshot',txid_current_snapshot()::text,'timezone',current_setting('TimeZone'));
    select row_to_json(z) from (select reservation_id,reserved_at,reserved_usd_micros,actual_usd_micros,
      task_id,node_id,model_profile_id,method,input_tokens,output_tokens,cache_read_tokens
      from model_usage where tenant_id='evals' and reserved_at between {start} and {end}
      order by reserved_at,reservation_id) z;
    commit;"""
    if replay is not None:
        if digest(replay/'proof.json') != expected_digest:
            raise ValueError('Frozen proof SHA-256 differs from the independent public receipt')
        previous=read(replay/'proof.json')
        if previous['ledger_window_utc'] != [iso(start),iso(end)] or previous['ledger_query_sha256'] != sha256(query.encode()).hexdigest():
            raise ValueError('Frozen query bounds or statement differ')
        if digest(replay/'ledger-query.sql') != previous['ledger_query_sha256'] or digest(replay/'ledger.jsonl') != previous['ledger_sha256']:
            raise ValueError('Frozen ledger bytes or SQL do not match the attested proof')
        with (replay/'ledger.jsonl').open() as stream:
            return previous['snapshot'],[json.loads(line) for line in stream if line.strip()],query
    out=subprocess.check_output(['kubectl','-n','data','exec','-i','pg-1','-c','postgres','--','psql',
                                 '-XqAt','-v','ON_ERROR_STOP=1','-d','agentruntime'],input=query.encode())
    records=[json.loads(line) for line in out.splitlines() if line]
    return records[0],records[1:],query

def main(output,replay=None,expected_digest=None):
    output.mkdir(mode=0o700,exist_ok=False,parents=False)
    owners=defaultdict(list); selected=[]; runs={}; excluded=defaultdict(Counter)
    for rid in RUNS:
        p=ARCHIVE/rid
        lock=read(p/'run-lock.json'); marker=read(p/'provider-quota-pause.json')
        buckets=read(p/'recovery-buckets-private.json')
        rows={hkey(to_key(r)):r for line in (p/'results.jsonl').open() if (r:=json.loads(line))}
        assert len(rows)==marker['observed_rows'] and digest(p/'results.jsonl')==marker['results_sha256'] and digest(p/'run-lock.json')==marker['run_lock_sha256']
        runs[rid]={'results_sha256':marker['results_sha256'],'run_lock_sha256':marker['run_lock_sha256'],
                   'buckets_sha256':digest(p/'recovery-buckets-private.json'),'retryable':{}}
        seen=set()
        for key in jobs(lock):
            h=hkey(key); assert h not in seen;seen.add(h)
            folder=p/'item-budgets'; budget=folder/(h+'.json'); state_lock=folder/(h+'.json.lock')
            target=buckets[h]=='quota_no_successful_call_recorded'
            if not state_lock.exists():
                if target:excluded[rid]['no_historical_lock_timestamp']+=1
                continue
            if not budget.is_file():
                raise RuntimeError('incomplete budget artifact with an existing owner lock')
            assert not state_lock.is_symlink() and not budget.is_symlink()
            earliest=state_lock.stat().st_mtime_ns//1_000_000
            last_write=(budget.stat().st_mtime_ns+999_999)//1_000_000
            if h in rows:
                last_possible=max(last_write,timestamp(rows[h]['at'])+999)
            else:
                last_possible=max(last_write,timestamp(marker['detected_at']))
            assert earliest<=last_write+1<=last_possible+1
            low,high=earliest-MIN_MARGIN_MS,last_possible+MIN_MARGIN_MS
            for task_model in possible_owners(key):
                owners[task_model].append((low,high,(rid,h)))
            if target:
                row=rows[h]; b=read(budget)
                assert to_key(row)==key and typed(row) and row.get('calls')=={} and row.get('cost_usd')==0
                assert type(b.get('spent_usd_micros')) is int and b['spent_usd_micros']==0
                assert b.get('pending') is True
                selected.append((rid,h,key,(earliest,last_write,low,high),expected_identity(row),budget))
        assert seen==set(buckets)
    left=min(z[3][2] for z in selected); right=max(z[3][3] for z in selected)
    meta,ledger,query=fetch_ledger(left,right,replay,expected_digest)
    assert len({a['reservation_id'] for a in ledger})==len(ledger)
    dates=[a['reserved_at'] for a in ledger]
    assert dates==sorted(dates)
    write(output/'ledger-query.sql',query)
    write(output/'ledger.jsonl',''.join(json.dumps(r,sort_keys=True)+'\n' for r in ledger))
    all_reserved=set()
    for rid,h,key,limits,expected,budget in selected:
        start,last,lo,hi=limits
        nearby=ledger[bisect_left(dates,lo):bisect_right(dates,hi)]
        if any(r['actual_usd_micros'] is None for r in nearby):
            excluded[rid]['unknown_settlement_in_expanded_tenant_window']+=1;continue
        if any(r['actual_usd_micros']!=0 for r in nearby):
            excluded[rid]['nonzero_settlement_in_expanded_tenant_window']+=1;continue
        if expected is None:
            excluded[rid]['no_unambiguous_target_rpc_signature']+=1;continue
        task,node,model=expected
        matches=[r for r in nearby if (r['task_id'],r['node_id'],r['model_profile_id'],r['method'])==(task,node,model,'generate')]
        if len(matches)!=1:
            excluded[rid]['signature_without_unique_reservation_in_window']+=1;continue
        r=matches[0]; t=r['reserved_at']
        if not start<=t<=last:
            excluded[rid]['no_match_inside_original_filesystem_interval']+=1;continue
        if any(r[k]!=0 for k in ('input_tokens','output_tokens','cache_read_tokens')):
            excluded[rid]['missing_or_nonzero_token_evidence']+=1;continue
        other={identity for a,b,identity in owners[(task,model)]+owners[(task,'*')] if a<=t<=b}
        if other!={(rid,h)}:
            excluded[rid]['overlap_with_another_potentially_matching_trial']+=1;continue
        reservation=r['reservation_id']; assert reservation not in all_reserved;all_reserved.add(reservation)
        runs[rid]['retryable'][h]={'key':key,'budget_sha256':digest(budget),'window_start_utc':iso(lo),'window_end_utc':iso(hi),
                                  'lock_mtime_millis_floor':start,'budget_mtime_millis_ceil':last,
                                  'reservation_id':reservation,'reservation_time_millis':t,
                                  'matched_task_id':task,'matched_node_id':node,'matched_model_profile':model,
                                  'all_tenant_reservations_in_window':len(nearby),'actual_usd_micros':0}
    for rid,data in runs.items():
        data['excluded_counts']=dict(excluded[rid]);data['candidate_quota_no_successful_count']=sum(r==rid for r,*_ in selected)
        assert data['candidate_quota_no_successful_count']==len(data['retryable'])+sum(excluded[rid].values())
    proof={'schema':SCHEMA,'tenant':'evals','margin_ms':MIN_MARGIN_MS,'snapshot':meta,
           'ledger_window_utc':[iso(left),iso(right)],'ledger_rows':len(ledger),'ledger_sha256':digest(output/'ledger.jsonl'),
           'ledger_query_sha256':digest(output/'ledger-query.sql'),'runs':runs}
    write(output/'proof.json',json.dumps(proof,indent=2,sort_keys=True)+'\n')
    if replay is not None and digest(output/'proof.json') != expected_digest:
        raise ValueError('Rederivation changed the proof; do not use these output files')
    summary={k:proof[k] for k in ('schema','tenant','margin_ms','snapshot','ledger_window_utc','ledger_rows','ledger_sha256','ledger_query_sha256')}
    summary['proof_sha256']=digest(output/'proof.json')
    summary['runs']={k:{'eligible':len(d['retryable']),'candidate_quota_no_successful_count':d['candidate_quota_no_successful_count'],
                        'excluded_counts':d['excluded_counts'],'results_sha256':d['results_sha256'],'run_lock_sha256':d['run_lock_sha256']} for k,d in runs.items()}
    write(output/'public-summary.json',json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))

if __name__=='__main__':
    args=argparse.ArgumentParser(description=__doc__);args.add_argument('output',type=Path)
    args.add_argument('--replay-ledger',type=Path);args.add_argument('--expected-proof-sha256')
    options=args.parse_args()
    if bool(options.replay_ledger) != bool(options.expected_proof_sha256):
        args.error('Pass both the frozen ledger directory and its independently recorded proof SHA-256')
    os.umask(0o077);main(options.output,options.replay_ledger,options.expected_proof_sha256)
