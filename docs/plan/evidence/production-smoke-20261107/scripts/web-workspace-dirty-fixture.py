#!/usr/bin/env python3
"""Write one isolated fixture through production Sandbox Service, without a Task."""
import argparse,json,os,re,subprocess
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--tenant',required=True);p.add_argument('--workspace',required=True);p.add_argument('--relative-path',required=True);p.add_argument('--expected-worker-image',required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
assert re.fullmatch(r'gh-goalaccept-[a-z0-9-]+',a.tenant)
assert re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}',a.workspace)
assert re.fullmatch(r'source/goal-acceptance-[a-f0-9]{16}\.txt',a.relative_path)
assert not a.out.exists()
env={**os.environ,'KUBECONFIG':'/etc/rancher/k3s/k3s.yaml'}
d=json.loads(subprocess.check_output(['kubectl','-n','agent-runtime','get','deployment','ar-agent-runtime-worker-program','-o','json'],env=env))
assert d['status']['observedGeneration']==d['metadata']['generation'] and d['status'].get('availableReplicas')==d['spec']['replicas']
assert d['spec']['template']['spec']['containers'][0]['image']==a.expected_worker_image
params={'tenant':a.tenant,'workspace':a.workspace,'path':a.relative_path}
script='PARAMS='+repr(params)+'\n'+r'''
import asyncio,dataclasses,json,os
from datetime import timedelta
from agentruntime_core.tenancy import TenantContext
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.tools import sandbox_broker
async def main():
 cfg=dataclasses.replace(WorkerConfig.from_env(os.environ),tool_workspace_root=None,sandbox_runtime='universal')
 broker=sandbox_broker(cfg);lease=None
 try:
  tenant=TenantContext(tenant_id=PARAMS['tenant'],workspace_id=PARAMS['workspace'],user_id='alice')
  lease=await broker.acquire(tenant,SandboxSpec(runtime='universal',workspace_id=PARAMS['workspace'],idle_timeout=timedelta(minutes=3)),task_id='fixture-dirty-detach',writable=True,reuse=False,busy_for=timedelta(minutes=2))
  await lease.box.write_file(lease.env_id,PARAMS['path'],b'isolated dirty detach acceptance\n')
  await broker.release(lease,keep=False);lease=None
  print(json.dumps({'fixture_written':True,'tenant':PARAMS['tenant'],'workspace':PARAMS['workspace'],'path':PARAMS['path'],'model_calls':0}))
 finally:
  if lease is not None:await broker.release(lease,keep=False,skip_snapshot=True)
  await broker.aclose();await broker.archiver._store.aclose()
asyncio.run(main())
'''
result=subprocess.run(['kubectl','-n','agent-runtime','exec','-i','deployment/ar-agent-runtime-worker-program','--','python','-'],input=script.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,timeout=180)
a.out.parent.mkdir(parents=True,exist_ok=True)
record={'returncode':result.returncode,'parameters':params,'model_calls':0}
if result.returncode:record['error']='fixture command failed; no stderr exported to prevent accidental credential disclosure'
else:
 record.update(json.loads(result.stdout.decode().strip().splitlines()[-1]))
a.out.write_text(json.dumps(record,indent=2)+'\n');a.out.chmod(0o600)
print(json.dumps(record));raise SystemExit(result.returncode)
