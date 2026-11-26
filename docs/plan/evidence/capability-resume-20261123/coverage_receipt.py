"""Summarize frozen observations without treating quota rejections as model errors."""
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
import hashlib,json,sys
from quota_snapshot import quota

base=Path('/opt/evals/deployments/capability-resume-20261123/paused-originals')
def per_candidate(rows, planned):
    accepted=[x for x in rows if not quota(x) and not x['contaminated'] and not x['error'] and
              x['pinned_calls']>0 and not x['missed_calls'] and x['calls'].get(x['role']+':'+x['candidate'],0)>0]
    data={}
    for name in sorted({x['eval'] for x in rows}):
        by_item=defaultdict(list);evidence=[x for x in accepted if x['eval']==name]
        for x in evidence:by_item[x['item']].append(x['score'])
        data[name]={'clean_trials':len(evidence),'unique_clean_items':len(by_item),
                    'clean_item_mean':sum(sum(a)/len(a) for a in by_item.values())/len(by_item) if by_item else None,
                    'draft_clean_trials':sum(x.get('record',{}).get('confirmed') is False for x in evidence)}
    return {'planned':planned,'receipts':len(rows),'clean_pinned':len(accepted),'quota_refusals':sum(quota(x) for x in rows),
            'known_spend_lower_bound_usd':round(sum(x['cost_usd'] for x in rows),6),
            'unknown_cost_rows':sum(x['cost_known'] is False for x in rows),'components':data}

out={}
for n in ('core','transform','judge-v2'):
    d=base/('capability-resume-20261123-'+n);p=d/'results.jsonl'
    rows=[json.loads(l) for l in p.read_text().split('\n') if l.strip()]
    lock=json.loads((d/'run-lock.json').read_bytes());plan=lock['plan'];cells=[]
    for c in plan['cells']:
        expected=0
        cell='/'.join((c.get('task_family',plan.get('task_family','general.unclassified')),c['role'],c['tier']))
        for raw in c['evals']:
            spec=raw if isinstance(raw,dict) else {'eval':raw}
            frozen=next(e for e in lock['evaluations'] if e['cell']==cell and e['spec']==spec['eval'])
            expected+=len(frozen['items'][:spec.get('n',c.get('n',plan['n']))])*spec.get('r',c.get('r',plan['r']))
        for mid in c['candidates']:
            mine=[r for r in rows if r['role']==c['role'] and r['candidate']==mid]
            cells.append({'role':c['role'],'tier':c['tier'],'model':mid,**per_candidate(mine,expected)})
    out[n]={'run':d.name,'results_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'cells':cells,
            'status':'paused','selected_after_observing_scores':False}
print(json.dumps(out,indent=2,ensure_ascii=False))
