"""Freeze the complete evals-eligible low-tier pool and exact public-document selection."""
import hashlib,json
from pathlib import Path
from collections import Counter

base=Path('/opt/evals/deployments/capability-resume-20261123')
models=json.loads((base/'profiles.json').read_text())['profiles']
pool=[m['id'] for m in models if m.get('availability') is not False and 'generate' in m['capabilities'] and any(t['name']=='low' for t in m['tiers'])]
assert len(pool)==9
index=next(Path('/opt/evals/data/longbench-v2').rglob('index.jsonl'))
rows=[json.loads(l) for l in index.read_text().split('\n') if l.strip()]
domains=['Financial','Governmental','Legal','Literary','Table QA']
eligible=[r for r in rows if r['length']=='short' and r['sub_domain'] in domains]
selected=sorted(eligible,key=lambda r:hashlib.sha256(('0\0'+r['id']).encode()).hexdigest())[:40]
assert len(eligible)==57 and len(selected)==40
cache=base/'longbench-document-40'; cache.mkdir(exist_ok=True)
(cache/'items').symlink_to(index.parent/'items',target_is_directory=True) if not (cache/'items').exists() else None
(cache/'index.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in selected))
selection={'source_revision':index.parent.name,'original_index_sha256':hashlib.sha256(index.read_bytes()).hexdigest(),
           'population':len(rows),'length':'short','subdomains':domains,'eligible':len(eligible),'seed':0,
           'selected_ids':[r['id'] for r in selected],'rule':'Ascending SHA-256 of UTF-8 seed NUL ID, first 40; no answer used for selection',
           'counts':dict(Counter(r['sub_domain'] for r in selected)),
           'selected_file_sha256':{r['id']:hashlib.sha256((cache/'items'/(r['id']+'.json')).read_bytes()).hexdigest() for r in selected}}
plan={'name':'capability-resume-20261123-transform','task_family':'general.unclassified','n':40,'r':3,'seed':0,
      'concurrency':8,'item_usd_limit':0.5,'extend':False,
      'cells':[{'role':'transform','tier':'low','candidates':pool,
                'evals':[{'eval':'s11','n':7,'r':3},{'eval':'longbench','n':40,'r':1,'options':{'cache':str(cache)}}]}]}
for name,obj in [('transform-plan.json',plan),('transform-selection.json',selection)]:
    output=base/name;encoded=json.dumps(obj,indent=2,ensure_ascii=False)+'\n'
    if output.exists() and output.read_text()!=encoded:raise ValueError('Input changed')
    output.write_text(encoded)
print('candidates',len(pool),'trials',len(pool)*(7*3+40))
