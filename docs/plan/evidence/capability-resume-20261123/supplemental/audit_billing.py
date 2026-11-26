"""Join supplemental receipts to the shared gateway ledger by unique opaque task identity."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from supplemental import DEPLOY, NAMES, PREFIX, SUPP, atomic, read, rows, sha

m={}
for n in NAMES:
    p=SUPP/(PREFIX+n)
    for slot,t in read(p/'attempts-private.json')['slots'].items():
        aid=t['proposed_attempt_id']
        assert re.fullmatch(r'mx2-[0-9a-f]{32}',aid) and aid not in m
        m[aid]=(n,slot)
ids_sql=','.join("'"+x+"'" for x in m)
sql=f"""begin transaction isolation level repeatable read read only;
select json_build_object('snapshot_at',clock_timestamp(),'transaction_snapshot',txid_current_snapshot()::text);
select row_to_json(x) from (select task_id,model_profile_id,method,count(*) as reservations,
count(actual_usd_micros) as settled,coalesce(sum(actual_usd_micros),0) as known_micros
from model_usage where tenant_id='evals' and task_id in ({ids_sql}) group by 1,2,3 order by 1,2,3) x;
commit;"""
raw=subprocess.check_output(['kubectl','-n','data','exec','-i','pg-1','-c','postgres','--','psql','-XqAt','-v','ON_ERROR_STOP=1','-d','agentruntime'],input=sql.encode())
records=[json.loads(l) for l in raw.splitlines() if l]
snapshot=records[0]
ledger=records[1:]
out={}
for n in NAMES:
    doc=read(SUPP/(PREFIX+n)/'attempts-private.json')
    rs=rows(SUPP/(PREFIX+n)/'results.jsonl')
    bucket=Counter()
    db_rows=[r for r in ledger if m[r['task_id']][0]==n]
    by_id={r['task_id'] for r in db_rows}
    receipt_ids={doc['slots'][s]['proposed_attempt_id'] for s in rs}
    with_target=0
    for s,r in rs.items():
        aid=doc['slots'][s]['proposed_attempt_id']
        bucket['has_gateway_reservation' if aid in by_id else 'no_gateway_reservation']+=1
        if any(x['task_id']==aid and x['model_profile_id']==r['candidate'] for x in db_rows):
            with_target+=1
    out[n]={'registered_attempts':len(doc['slots']),'receipted_attempts':len(rs),'attempts_with_any_gateway_reservation':len(by_id),
            'receipts_with_exact_billing_id':bucket['has_gateway_reservation'],'receipts_with_target_profile_in_ledger':with_target,
            'inflight_or_aborted_ids_with_ledger_but_no_receipt':len(by_id-receipt_ids),
            'reservations':sum(int(r['reservations']) for r in db_rows),'settled_reservations':sum(int(r['settled']) for r in db_rows),
            'settled_usd':sum(int(r['known_micros']) for r in db_rows)/1e6,
            'raw_lower_bound_usd':round(sum(r.get('cost_usd') or 0 for r in rs.values()),6),
            'ledger_model_profiles':sorted({r['model_profile_id'] for r in db_rows})}
private=DEPLOY/'supplemental-code'/'ledger-aggregates-private.json'
atomic(private,{'snapshot':snapshot,'rows':ledger})
print(json.dumps({'schema':'matrix-supplement-billing-v1','snapshot':snapshot,
    'private_ledger_digest':sha(private),'grouped':out},indent=2))
