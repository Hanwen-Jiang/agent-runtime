"""Freeze the three additional matrix roles before issuing any model call."""
from pathlib import Path
from collections import Counter
import hashlib,json
import core_cohorts as c

D=Path('/opt/evals/deployments/capability-resume-20261123')
here=D/'core'
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def save(name,doc):
    p=here/name;s=json.dumps(doc,indent=2,ensure_ascii=False)+'\n'
    if p.exists() and p.read_text()!=s:raise ValueError('frozen '+name+' changed')
    p.write_text(s)
catalog=json.loads((D/'profiles.json').read_bytes())
low=json.loads((D/'transform-plan.json').read_bytes())['cells'][0]['candidates']
strong=json.loads((D/'judge/plan.json').read_bytes())['cells'][0]['candidates']
assert len(low)==9 and len(strong)==3
specs=[('plan',c.plan,40,3,strong,'medium'),('act',c.act,100,1,strong,'medium'),('route',c.route,40,3,low,'low')]
meta={};cells=[]
for role,make,n,r,models,tier in specs:
    e=make();items=e.items(n,0)
    assert len(items)==n and len({i.id for i in items})==n
    cells.append({'role':role,'tier':tier,'evals':[{'eval':'core_cohorts:'+role,'n':n,'r':r}],'candidates':models})
    meta[role]={'evaluator':e.name,'version':e.version,'sample_count':n,'repeats':r,'models':models,
                'ordered_ids_sha256':hashlib.sha256(canonical([i.id for i in items])).hexdigest()}
    if role=='act':
        meta[role].update(original=len(c.bfcl.make().rows()),eligible=len(e.rows()),
                          method='function-name business allowlist on every offered schema, then native seed=0 stratified selection',
                          categories=dict(Counter(i.payload['category'] for i in items)),ids=[i.id for i in items])
    else:
        cases=[e.cases[i.id] for i in items]
        meta[role].update(eligible=len(e.cases),candidate_pool_confirmed=sum(x.confirmed for x in e.cases.values()),
                          selected_confirmed=sum(x.confirmed for x in cases),selected_draft=sum(not x.confirmed for x in cases),
                          method='all confirmed S09 first, then NATURAL PLAN / TravelPlanner drafts, seed=0 within strata; private holdout IDs withheld',
                          split_counts=dict(Counter(x.split for x in cases)),
                          public_adapted_ids=[i.id for i in items if i.id.startswith('adapted:')])
    meta[role]['trials']=n*r*len(models)
plan={'name':'capability-resume-20261123-core','n':40,'r':3,'seed':0,'concurrency':12,
      'schedule':'round_robin','extend':False,'item_usd_limit':8.0,'cells':cells}
save('plan.json',plan); save('selection.json',meta)
base_release=json.loads((D/'release.json').read_bytes())
freeze=json.loads((D/'freeze.json').read_bytes())
files=['services/py-learning/src/agentruntime_learning/capability/'+x for x in ['matrix.py','publication.py']]
release={'shared_deployment':base_release,'bench_only_source_deltas':{x:sha(here/'src'/x) for x in files},
         'custom_selection_module_sha256':sha(here/'core_cohorts.py')}
freeze['deployment_manifest_sha256']=hashlib.sha256(canonical(release)).hexdigest()
save('release.json',release);save('freeze.json',freeze)
print({k:(v['eligible'],v['sample_count'],v['trials']) for k,v in meta.items()})
