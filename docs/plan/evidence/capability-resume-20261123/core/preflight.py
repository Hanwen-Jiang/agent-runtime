"""Run one disjoint native example per planned role and profile to verify the cap and pin."""
import asyncio, hashlib, json
from pathlib import Path
import core_cohorts
from agentruntime_learning.evals.judging import gateway_from_env
from agentruntime_learning.capability.base import PinningGateway
from agentruntime_learning.capability.matrix import _ItemBudget

D=Path('/opt/evals/deployments/capability-resume-20261123')
here=D/'core'
plan=json.loads((here/'plan.json').read_bytes())
classes={p['id']:p['class'] for p in json.loads((D/'profiles.json').read_bytes())['profiles']}

async def main():
    gw=gateway_from_env()
    gate=asyncio.Semaphore(8)
    out=here/'calibration';out.mkdir(exist_ok=True)
    async def run(model,role,tier,ev,item):
        test=hashlib.sha256((role+model).encode()).hexdigest()[:16]
        output=out/(test+'.json')
        if output.exists():return json.loads(output.read_bytes())
        async with gate:
            budget=_ItemBudget(gw,plan['item_usd_limit'],out/(test+'-budget.json'))
            pin=PinningGateway(budget,role=role,candidate=model,tier=classes[model]+'@'+tier)
            try:
                result=await ev.run_item(item,pin,candidate=model)
                row={'role':role,'model':model,'item_sha256':hashlib.sha256(item.id.encode()).hexdigest(),'tier':classes[model]+'@'+tier,
                     'error':result.error,'exceeded':budget.exceeded,'known':budget.cost_known,'pinned':pin.pinned_calls,'missed':pin.missed_calls,
                     'actual_calls':{f'{a}:{b}':v for (a,b),v in pin.calls.items()},'reported_usd':budget.spent}
            finally:
                budget.close()
            output.write_text(json.dumps(row,indent=2)+'\n')
            return row
    jobs=[]
    for cell in plan['cells']:
        role=cell['role'];ev=getattr(core_cohorts,role)();n=cell['evals'][0]['n'];item=ev.items(n+1,0)[n]
        for model in cell['candidates']:jobs.append(run(model,role,cell['tier'],ev,item))
    results=await asyncio.gather(*jobs)
    (here/'preflight.json').write_text(json.dumps(results,indent=2)+'\n')
    for c in plan['cells']:
        rows=[r for r in results if r['role']==c['role']]
        if not any(r['pinned'] and r['missed']==0 and not r['exceeded'] for r in rows):
            raise ValueError('No successful pin for role '+c['role']+'; examine calibration before starting')
    print('Preflight complete',len(results),flush=True)
if __name__=='__main__':asyncio.run(main())
