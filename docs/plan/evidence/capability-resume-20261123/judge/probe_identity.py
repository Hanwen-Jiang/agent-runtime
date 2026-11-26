import asyncio, json
from pathlib import Path
from datetime import datetime, timezone
from agentruntime_learning.capability.evals._datasets import Meter,chat
from agentruntime_learning.evals.judging import gateway_from_env
keys=['a6api/claude-opus-5-5@2026-10','a6api/claude-fable-5-1@2026-11','a6api/gpt-6-astra@2026-11']
d=Path('/opt/evals/deployments/capability-resume-20261123')
async def run():
 gw=gateway_from_env(); prof={p['id']:p for p in json.loads((d/'profiles.json').read_bytes())['profiles']}
 async def probe(k):
  try:
   tier=prof[k]['class']+'@medium'
   result=await asyncio.wait_for(Meter(gw,item_id=k.split('/')[1],eval_name='capability-judge-preflight-20261123').generate(role='judge',node='identity',messages=[chat('user','Return just the integer result of 1 + 1.')],tier=tier,pinned=k,max_output_tokens=128),120)
   return {'requested':k,'tier':tier,'served':result.model.model_profile_id,'settled_usd':result.usage.cost_usd_micros/1e6,'received_text':bool(result.text.strip())}
  except Exception as e:
   return {'requested':k,'error_type':type(e).__name__,'error':str(e)[:450]}
 return {'at':datetime.now(timezone.utc).isoformat(),'items':await asyncio.gather(*(probe(k) for k in keys))}
print(json.dumps(asyncio.run(run()),indent=2))
