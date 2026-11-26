"""Replay only observed base-stage records; never infer scores for missing attempts."""
import argparse
from collections import Counter, defaultdict, deque
import hashlib
import json
from pathlib import Path


def interleave(groups):
    queue=deque(iter(g) for g in groups)
    while queue:
        group=queue.popleft()
        try:
            item=next(group)
        except StopIteration:
            continue
        yield item
        queue.append(group)


def metrics(rows):
    lane=lambda r:(r['task_family'],r['role'],r['tier'],r['eval'],r['candidate'])
    valid=[r for r in rows if not r['error'] and not r['contaminated'] and r['pinned_calls']>0 and r['missed_calls']==0
           and r['calls'].get(r['role']+':'+r['candidate'],0)>0]
    return {'attempts':len(rows),'roles':len({r['role'] for r in rows}),'lanes':len({lane(r) for r in rows}),
            'lanes_with_valid_pin':len({lane(r) for r in valid}), 'valid_pinned_attempts':len(valid),
            'reported_cost_usd':round(sum(r['cost_usd'] for r in rows),6),
            'explicitly_unknown_cost_records':sum(r.get('cost_known') is False for r in rows),
            'missing_cost_known_field':sum('cost_known' not in r for r in rows)}


def summarize(folder, cutoffs):
    plan=json.loads((folder/'plan.json').read_bytes())
    roles={(c.get('task_family',plan.get('task_family','general.unclassified')),c['role'],c['tier']):c for c in plan['cells']}
    source=folder/'results.jsonl'
    entire=[json.loads(l) for l in source.read_text().split('\n') if l.strip()]
    history=defaultdict(dict); seen=set(); base=[]
    for row in entire:
        role=(row['task_family'],row['role'],row['tier']); lane=(row['eval'],row['candidate'])
        key=(*role,*lane,row['item'],row['repeat'])
        if key in seen:
            raise ValueError('duplicate observation')
        seen.add(key); cell=roles[role]
        if row['candidate'] in cell.get('expensive',[]) and set(cell['candidates'])-set(cell['expensive']):
            initial=history[role].setdefault(lane,[])
            if row['repeat']!=0 or len(initial)>=plan.get('tournament_n',20):
                continue
        history[role].setdefault(lane,[]).append(row);base.append(row)
    fair=list(interleave(interleave(history[k].values()) for k in roles if k in history))
    assert set(map(id,base))==set(map(id,fair))
    return {'plan':plan['name'],'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'all_observed':len(entire),'observed_base_stage':len(base),'sample_unit':'attempt in archived receipt sequence, not a model call',
            'available':metrics(base),'prefixes':{str(n):{'sequential_record_order':metrics(base[:n]),'round_robin':metrics(fair[:n])} for n in cutoffs}}


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('run',type=Path);ap.add_argument('n',type=int,nargs='+');a=ap.parse_args()
    print(json.dumps(summarize(a.run,a.n),indent=2))
