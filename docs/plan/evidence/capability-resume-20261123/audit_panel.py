"""Audit scheduled attempts, actual pinning, cost evidence and publication without reading prompts."""
import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
import re
from pathlib import Path

RUNS = ('capability-resume-20261123-core','capability-resume-20261123-judge-v2','capability-resume-20261123-transform')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def quota_denied(row):
    error = row.get("error") or ""
    model_errors = (row.get("record") or {}).get("model_call_failures") or {}
    return bool(re.search(r"\binsufficient_user_quota\b", error)) or bool(model_errors.get("insufficient_user_quota"))


def failure_category(row):
    if quota_denied(row):
        return "insufficient_user_quota"
    labels = (row.get("record") or {}).get("model_call_failures") or {}
    if labels:
        return " + ".join(sorted(label for label in labels if re.fullmatch(r"[A-Za-z0-9_]{1,64}",label))) or "model_call_failure"
    text = row.get("error") or ""
    match = re.match(r"(?:[A-Za-z]+Error: )?([A-Za-z][A-Za-z0-9_]{0,63})(?::|$)", text)
    return match.group(1) if match else "pin_or_quality_contamination"


def audit(root, strict):
    slots = []
    sources = {}
    ids = set()
    envs = []
    for rid in RUNS:
        p = root/rid
        lock = read(p/'run-lock.json')
        plan = read(p/'plan.json')
        assert plan == lock['plan'] and plan['name'] == rid
        envs.append({k:v for k,v in lock['deployment'].items() if k!='deployment_manifest_sha256'})
        with (p/'results.jsonl').open() as f:
            rows = [json.loads(l) for l in f if l.endswith('\n')]
        source = {k:sha(p/k) for k in ('run-lock.json','plan.json','results.jsonl')}
        if strict:
            scores = read(p/'scores.json')
            source['scores.json'] = sha(p/'scores.json')
        else:
            scores = {'cells':[]}
        sources[rid] = source
        for c in plan['cells']:
            cell='/'.join([c.get('task_family',plan.get('task_family','general.unclassified')),c['role'],c['tier']])
            evaluators = [e for e in lock['evaluations'] if e['cell']==cell]
            expected = set()
            for e in evaluators:
                spec=next(s for s in c['evals'] if (s.get('eval') if isinstance(s,dict) else s)==e['spec'])
                repeat=spec.get('r',c.get('r',plan['r'])) if isinstance(spec,dict) else c.get('r',plan['r'])
                assert len(set(e['items'])) == len(e['items'])
                expected.update((e['name'],i,r) for i in e['items'] for r in range(repeat))
            for candidate in c['candidates']:
                match = [r for r in rows if r['role']==c['role'] and r['tier']==c['tier'] and r['candidate']==candidate]
                seen = {(r['eval'],r['item'],r['repeat']) for r in match}
                assert len(match)==len(seen) and seen<=expected
                assert (cell,candidate) not in ids
                ids.add((cell,candidate))
                if strict:
                    assert seen==expected, (rid,cell,candidate,len(expected-seen))
                    cell_score=next(cs for cs in scores['cells'] if cs['role']==c['role'] and cs['tier']==c['tier'])
                    s=next(x for x in cell_score['candidates'] if x['candidate']==candidate)
                    assert s['runs']==len(match)
                else:
                    s={}
                bad=Counter()
                for r in match:
                    if r.get('contaminated') or r.get('error'):
                        bad[failure_category(r)]+=1
                ok=[r for r in match if not r.get('contaminated') and not r.get('error')]
                pinned=[r for r in ok if r.get('pinned_calls',0)>0 and not r.get('missed_calls') and r.get('calls',{}).get(c['role']+':'+candidate,0)>0]
                quota = [r for r in match if quota_denied(r)]
                slots.append({'quota_denied':len(quota),'quota_with_returned_call':sum(bool(r.get('calls')) for r in quota),'role':c['role'],'tier':c['tier'],'candidate':candidate,'source':rid,'planned_attempts':len(expected),
                              'attempts':len(match),'clean_scoring_rows':len(ok),'clean_rows_with_pin_evidence':len(pinned),
                              'rows_with_cost_unknown':sum(r.get('cost_known') is not True for r in match),
                              'recorded_usd':float(sum((Decimal(str(r['cost_usd'])) for r in match if r.get('cost_usd') is not None),Decimal(0))),
                              'quality_failures':dict(bad),'status':s.get('status'),'publishable':s.get('publishable'),
                              'raw_score':s.get('p'),'adjusted_score':s.get('score'),'independent_item_ids_with_scores':s.get('items')})
    assert all(envs[0]==e for e in envs[1:]), 'Different executing services/model registries'
    assert len(ids)==27 and len({k for k,_ in ids})==5
    return {'scope':'2026-11-23 representative panel','sources':sources,'slots':slots,'planned':sum(s['planned_attempts'] for s in slots),'observed':sum(s['attempts'] for s in slots),'clean_scoring_rows':sum(s['clean_scoring_rows'] for s in slots),'clean_rows_with_pin_evidence':sum(s['clean_rows_with_pin_evidence'] for s in slots),'quota_denied':sum(s['quota_denied'] for s in slots),'quota_with_returned_call':sum(s['quota_with_returned_call'] for s in slots),'slots_with_clean_pin':sum(s['clean_rows_with_pin_evidence']>0 for s in slots),'recorded_usd':round(sum(s['recorded_usd'] for s in slots),6),'rows_with_cost_unknown':sum(s['rows_with_cost_unknown'] for s in slots),'publication_eligible_slots':sum(s['publishable'] is True for s in slots) if strict else None,'all_planned_attempts_checked':strict,'shared_runtime_and_registry_sha256':digest(envs[0])}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('/opt/evals/mx'))
    parser.add_argument('--final',action='store_true')
    a=parser.parse_args()
    print(json.dumps(audit(a.root,a.final),ensure_ascii=False,indent=2))
