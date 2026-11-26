from pathlib import Path
import sys,json,hashlib
from agentruntime_learning.capability import matrix,quota
from agentruntime_learning.capability.__main__ import _load

D=Path('/opt/evals/deployments/capability-resume-20261123')
role=sys.argv[1]; rid='capability-resume-20261123-'+role
real=Path('/opt/evals/mx')/rid
scratch=Path(sys.argv[2])/rid
try:quota.require_running(real)
except quota.ProviderQuotaPaused:pass
else:raise ValueError('canonical run was not paused')
rec=json.loads((scratch/'resume-reconciliation.json').read_bytes())
plan,profiles,evals=_load(scratch/'plan.json',D/'profiles.json')
freeze=D/'core/freeze.json' if role=='core' else D/'freeze.json'
with matrix.output_owner(scratch) as owner:
    runner=matrix.MatrixRunner(plan,object(),scratch,profiles=profiles,evals=evals,
       frozen=json.loads(freeze.read_bytes()),registry_revisions=json.loads((D/'profiles.json').read_bytes())['registry_revisions'],owner=owner)
    pending=0;held=0
    for c in plan.cells:
        for job in runner._jobs(c,c.candidates,{e.spec:(e.n,e.r) for e in c.evals}):
            k=(c.key,job.ev.name,job.candidate,job.item.id,job.repeat)
            path=scratch/'item-budgets'/(hashlib.sha256(json.dumps(k).encode()).hexdigest()+'.json')
            state=json.loads(path.read_bytes()) if path.exists() else {}
            if state.get('pending') or state.get('spent_usd_micros',0)>0:held+=1
            else:pending+=1
    assert pending==rec['reopened_proven_zero']+rec['unattempted_no_observed_spend']
    assert held==rec['held_unreceipted']
    assert quota.file_sha(real/'run-lock.json')==quota.file_sha(scratch/'run-lock.json')
    assert quota.file_sha(real/'results.jsonl')==rec['source_results_sha256']
    print(json.dumps({'run':rid,'no_provider_calls':True,'canonical_paused':True,'frozen_run_lock_match':True,
              'preserved_receipts':len(runner.rows),'ready_after_confirmed_funding':pending,'no_receipt_held':held,
              'all_original_results_unchanged':True,'original_results_sha256':rec['source_results_sha256']}))
    runner.close()
