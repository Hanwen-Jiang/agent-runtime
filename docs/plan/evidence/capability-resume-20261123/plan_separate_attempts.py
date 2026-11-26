"""Pre-register slots requiring a separate attempt if billing cannot later be attributed."""
from collections import Counter
from pathlib import Path
import hashlib,json
from prepare_quota_resume import DEPLOY,PROOF_SHA

proofpath=DEPLOY/'quota-ledger-proof'/'proof.json'
assert hashlib.sha256(proofpath.read_bytes()).hexdigest()==PROOF_SHA
proof=json.loads(proofpath.read_bytes());summary={}
for rid,run in sorted(proof['runs'].items()):
    archive=DEPLOY/'paused-originals'/rid
    buckets=json.loads((archive/'recovery-buckets-private.json').read_bytes())
    confirmed=set(run['retryable']);slots=[]
    for slot,bucket in sorted(buckets.items()):
        if slot in confirmed or bucket not in ('quota_no_successful_call_recorded','quota_after_provider_observation','no_receipt_preserve_reservation_or_spend'):
            continue
        attempt='mx2-'+hashlib.sha256((rid+'\0'+slot+'\0attempt-2').encode()).hexdigest()[:32]
        slots.append({'original_slot_sha256':slot,'proposed_attempt_id':attempt,'original_billing_state':bucket})
    data={'schema':'matrix-quota-supplement-design-v1','parent_run':rid,'original_results_sha256':run['results_sha256'],
          'run_lock_sha256':run['run_lock_sha256'],'ledger_zero_proof_sha256':PROOF_SHA,'model_calls':0,
          'selection_rule':'exclude every valid or otherwise final non-quota record, every proven-zero safe retry and all never-started slots',
          'score_rule':'At most one prospectively authorised separate attempt for any still-unresolved slot. Retain original spend and evidence, keep the original dataset denominator, report separate-attempt sensitivity. No best-of selection.',
          'slots':slots}
    encoded=(json.dumps(data,sort_keys=True,indent=2)+'\n').encode()
    out=archive/'future-separate-attempts-private.json'
    if out.exists() and out.read_bytes()!=encoded:raise ValueError('proposed attempt set changed')
    out.write_bytes(encoded);out.chmod(0o600)
    summary[rid]={'proposed':len(slots),'by_reason':dict(Counter(x['original_billing_state'] for x in slots)),
                  'source_run_lock_sha256':run['run_lock_sha256'],'manifest_sha256':hashlib.sha256(encoded).hexdigest(),'model_calls':0}
print(json.dumps(summary,indent=2))
