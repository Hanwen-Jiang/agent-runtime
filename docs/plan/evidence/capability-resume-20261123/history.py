"""Check observed plan slots from the original judge and gateway observations, without exporting prompts."""
from pathlib import Path
from collections import Counter
from decimal import Decimal
import hashlib
import json

p=Path('/opt/evals/mx/round1')
plan=json.loads((p/'plan.json').read_bytes())
scores=json.loads((p/'scores.json').read_bytes())
with (p/'results.jsonl').open() as f:
    rows=[json.loads(line) for line in f if line.strip()]
counts=Counter((r['role'],r['tier'],r['candidate']) for r in rows)
keys={(r['role'],r['tier'],r['candidate'],r['eval'],r['item'],r['repeat']) for r in rows}
index={(c['role'], c['tier']):c for c in scores['cells']}
out={}
for cell in plan['cells']:
    role,tier=cell['role'],cell['tier']
    score={c['candidate']:c for c in index[role,tier]['candidates']}
    assert set(cell['candidates'])==set(score)
    stats={}
    for model in cell['candidates']:
        n=counts[role,tier,model]; s=score[model]
        assert n==s['runs']
        stats[model]={'records':n,'distinct_scored_items':s['items'],'historical_status':s['status']}
    out[role]={'tier':tier,'planned_slots':len(stats),'observed_slots':sum(x['records']>0 for x in stats.values()),'candidates':stats}
print(json.dumps({'plan_id':plan['name'],'slots':out,'observation_rows':len(rows),'unique_keys':len(keys),
      'recorded_usd':str(sum(Decimal(str(r.get('cost_usd') or 0)) for r in rows)),
      'sha256':{n:hashlib.sha256((p/n).read_bytes()).hexdigest() for n in ['plan.json','results.jsonl','scores.json']}},indent=2))
