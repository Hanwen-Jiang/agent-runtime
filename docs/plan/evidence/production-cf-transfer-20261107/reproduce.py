"""Single isolated zero-model CF acceptance; run only in the deployed Worker."""
import asyncio,dataclasses,hashlib,json,os,secrets,time
from datetime import timedelta
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.tenancy import TenantContext
from agentruntime.registry.v1 import registry_pb2
from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.registry.client import GrpcRegistry
from agentruntime_worker.tools import sandbox_broker,context_objects,dev_tools,_relational,_platform_secret

async def main():
 if os.environ.get('AR_CF_ACCEPT_EXECUTE')!='1':raise RuntimeError('production acceptance gate required')
 nonce=secrets.token_hex(8);owner='cf-accept-'+nonce;task='task-cf-'+nonce;started=time.time()
 cfg=dataclasses.replace(WorkerConfig.from_env(os.environ),tool_workspace_root=None,sandbox_sessions=False,sandbox_runtime='universal')
 broker=sandbox_broker(cfg);objects=context_objects(cfg);bucket=objects.bind(TenantContext(tenant_id=owner));db=_relational(cfg);scoped=db.bind(TenantContext(tenant_id=owner))
 token=await _platform_secret(cfg,'registry/publish-token');assert token
 registry=GrpcRegistry(cfg.registry_address,publish_token=token.decode())
 tc=TenantContext(tenant_id=owner,user_id='acceptance')
 report={'tenant':owner,'task':task,'started_unix':started,'model_calls':0,'model_cost_usd':0,'cloudflare_infrastructure_cost_usd':None,'checks':{},'cleanup':{}}
 print(json.dumps({'event':'acceptance_started','tenant':owner,'task':task,'started_unix':started}),flush=True)
 async def rows(sql,*args):
  async with scoped.transaction(read_only=True) as tx:return await tx.query(sql,args)
 failure=None;tools=None
 try:
  policy={'model_allowlist':[],'model_budget':{'daily_usd':0,'monthly_usd':0},'sandbox':{'backend':'cloudflare','fallback':[],'min_isolation':'microvm','idle_timeout_s':180,'max_lifetime_s':300,'quota':{'max_concurrent':1,'daily_sandbox_seconds':300,'daily_egress_bytes':100000},'network':{'allowed':False}},'workspace':{'max_workspaces':1,'max_bytes':100000}}
  pub=await registry.publish(Tenant(tenant_id=owner),registry_pb2.REGISTRY_KIND_TENANT_POLICY,'',policy,change_note='isolated zero-model CF scoped transfer acceptance')
  report['policy_revision']=pub.version
  lease=await broker.acquire(tc,SandboxSpec(runtime='universal',network='none',idle_timeout=timedelta(minutes=3)),task_id=task,writable=False,reuse=False,busy_for=timedelta(minutes=1))
  assert lease.backend.kind=='cloudflare'
  report['environment_id']=lease.env_id
  identity=await lease.box.exec(lease.env_id,['sha256sum','/usr/local/bin/sandbox-agent'],timeout=timedelta(seconds=10))
  assert identity.exit_code==0
  live_sha=identity.stdout.decode().split()[0]
  assert live_sha=='795c96f868594cdc9bf62d0379dc11d2cf767bac09c17cb8a7dfcfd037fce7a5'
  report['live_agent_sha256']=live_sha
  text=(nonce*1250).encode();assert len(text)==20000
  result=await lease.box.exec_with_output_refs(lease.env_id,['python3','-c','import sys;sys.stdout.write(sys.argv[1]*1250)',nonce],timeout=timedelta(seconds=30))
  assert result.exit_code==0 and len(result.stdout)<9000 and result.stdout_object and not result.stdout_pending
  ref=result.stdout_object
  assert ref['size_bytes']==len(text) and ref['sha256']==hashlib.sha256(text).hexdigest()
  obj=await bucket.get(bucket.key_of(ref['object_uri']));assert obj.data==text
  tools=dev_tools(cfg)
  out=(await tools.bind(tc).invoke('context_read','v1',{'path':ref['object_uri'],'offset':107,'max_chars':123},task_id=task)).output
  assert out['content']==text.decode()[107:230]
  report['checks']['direct_output']={'total_bytes':len(text),'worker_excerpt_bytes':len(result.stdout),'sha256':ref['sha256'],'context_read_verified':True}
  accounting=await rows('SELECT grant_id,reserved_bytes,actual_bytes,acknowledged,task_id FROM sandbox_transfer_usage')
  assert len(accounting)==1 and accounting[0]['task_id']==task
  assert int(accounting[0]['reserved_bytes'])==60000 and int(accounting[0]['actual_bytes'])==20000 and int(accounting[0]['acknowledged'])==1
  report['transfer_accounting']=accounting
  usage=await rows('SELECT day,egress_bytes FROM sandbox_usage');assert sum(int(r['egress_bytes']) for r in usage)==20000
  report['checks']['quota_settlement']={'reserved_bytes':60000,'actual_payload_bytes':20000,'pending':0,'acknowledged':True,'excludes_transport_overhead':True}
  model=await rows('SELECT reservation_id FROM model_usage');assert not model
  report['checks']['zero_model_usage']=True
 except BaseException as error:
  failure=error;report['failure']={'type':type(error).__name__,'code':getattr(error,'code',None),'message':str(error)[:600]}
 finally:
  try:
   await broker.destroy_tenant(owner)
   live=await rows("SELECT env_ref FROM sandbox_envs WHERE state IN ('ready','creating','starting','destroying')");assert not live
   report['cleanup']['envs_destroyed']=True
   report['final_usage']=await rows('SELECT * FROM sandbox_usage')
   pending=await rows('SELECT grant_id FROM sandbox_transfer_usage WHERE actual_bytes IS NULL OR acknowledged=0');assert not pending
   deleted=0
   while True:
    page=await bucket.list(prefix='',limit=1000)
    for item in page.items:await bucket.delete(item.key);deleted+=1
    if not page.items:break
   assert not (await bucket.list(prefix='',limit=1)).items
   report['cleanup']['objects_deleted']=deleted
   for table in ['sandbox_transfer_usage','sandbox_transfers','task_artifacts','task_artifact_reservations','task_artifact_quota_lock','workspace_snapshots','workspace_pushes','workspaces','sandbox_grants','sandbox_egress_audit','sandbox_envs','sandbox_usage','sandbox_quota_lock','registry_entries']:
    async with scoped.transaction() as tx:await tx.execute('DELETE FROM '+table)
   async with db.bind_platform('cf-acceptance-cleanup').transaction() as tx:await tx.execute("DELETE FROM sandbox_env_directory WHERE owner=$1 AND state IN ('destroyed','failed','lost')",(owner,))
   report['cleanup']['scoped_rows_deleted']=True
  except BaseException as error:
   report['cleanup']['failure']={'type':type(error).__name__,'message':str(error)[:600]};failure=failure or error
  await broker.aclose();await registry.aclose()
  stores={id(db):db,id(broker.archiver._store):broker.archiver._store}
  if tools is not None:
   for group in tools._tools.values():
    for tool in group.values():
     candidate=getattr(tool.handler,'store',None)
     if candidate is not None and hasattr(candidate,'aclose'):stores[id(candidate)]=candidate
  for store in stores.values():await store.aclose()
  report['elapsed_seconds']=round(time.time()-started,3);report['passed']=failure is None
  print(json.dumps(report,ensure_ascii=False,default=str),flush=True)
 return 1 if failure else 0
if __name__=='__main__':raise SystemExit(asyncio.run(main()))
