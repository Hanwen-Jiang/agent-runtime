import asyncio,dataclasses,hashlib,json,os,secrets,time
from datetime import timedelta
from urllib.parse import urlsplit
from agentruntime.registry.v1 import registry_pb2
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.tenancy import TenantContext
from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.registry.client import GrpcRegistry
from agentruntime_worker.tools import sandbox_broker,context_objects,_relational,_platform_secret
from agentruntime_worker.sandbox.grants import GrantStore,NetworkRequest,NetworkPolicy

CURL_PROBE=r'''
import os,sys,json,tempfile,subprocess,hashlib
payload=json.load(sys.stdin)
with tempfile.NamedTemporaryFile() as f:
 f.write(bytes.fromhex(payload['data_hex']));f.flush()
 def escaped(s):return s.replace('\\','\\\\').replace('"','\\"')
 cfg='url = "'+escaped(payload['url'])+'"\n'
 for k,v in payload['headers'].items():cfg+='header = "'+escaped(k+': '+v)+'"\n'
 proxy=os.environ.get('HTTPS_PROXY','')
 if proxy:cfg+='proxy = "'+escaped(proxy)+'"\n'
 cmd=['/usr/bin/curl','--disable','--silent','--show-error','--globoff','--fail','--proto','=http,https','--connect-timeout','10','--max-time','30','--config','-','--request','PUT','--upload-file',f.name,'--output','/dev/null','--write-out','%{http_code} %{ssl_verify_result} %{http_connect}']
 r=subprocess.run(cmd,input=cfg.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={'PATH':'/usr/bin:/bin'},timeout=35)
 # Neither signed URL, proxy auth nor raw response is ever returned.
 markers=[name for name,needle in [('certificate','certificate'),('empty_reply','Empty reply'),('recv_failure','Recv failure'),('http_error','requested URL returned error'),('timeout','timed out'),('connect','CONNECT')] if needle.lower() in r.stderr.decode(errors='replace').lower()]
 print(json.dumps({'size':len(bytes.fromhex(payload['data_hex'])),'curl_exit':r.returncode,'codes':r.stdout.decode()[:80],'error_classes':markers,'proxy_present':bool(proxy),'stderr_bytes':len(r.stderr)}))
'''

async def main():
 owner='gh-goalkata-debug-'+secrets.token_hex(5);task='diag-'+secrets.token_hex(8);ws='diag'
 print(json.dumps({'event':'start','tenant':owner,'task':task}),flush=True)
 cfg=dataclasses.replace(WorkerConfig.from_env(os.environ),tool_workspace_root=None,sandbox_runtime='universal')
 broker=sandbox_broker(cfg);objects=context_objects(cfg);bucket=objects.bind(TenantContext(tenant_id=owner));store=_relational(cfg);scope=store.bind(TenantContext(tenant_id=owner))
 token=await _platform_secret(cfg,'registry/publish-token');registry=GrpcRegistry(cfg.registry_address,publish_token=token.decode());grants=GrantStore(store)
 report={'tenant':owner,'task':task,'rows':[],'cleanup':{},'model_calls':0};failure=None
 try:
  first=await bucket.signed_upload('probe-authority',ttl=timedelta(minutes=3),sha256=hashlib.sha256(b'x').hexdigest(),size=1);host=urlsplit(first.url).hostname
  policy={'model_allowlist':[],'model_budget':{'daily_usd':0,'monthly_usd':0},'sandbox':{'backend':'kata','fallback':[],'min_isolation':'microvm','idle_timeout_s':120,'max_lifetime_s':600,'quota':{'max_concurrent':1,'daily_sandbox_seconds':600,'daily_egress_bytes':33554432},'network':{'allowed':True,'hosts':[host,'github.com'],'max_grant_minutes':15}},'workspace':{'max_workspaces':1,'max_bytes':33554432}}
  await registry.publish(Tenant(tenant_id=owner),registry_pb2.REGISTRY_KIND_TENANT_POLICY,'',policy,change_note='isolated zero-model Kata transport diagnosis')
  await grants.create(owner,NetworkRequest(hosts=(host,'github.com'),ports=(443,),minutes=5,reason='scoped transfer diagnosis'),NetworkPolicy(allowed=True,hosts=(host,),max_up_bytes=8388608,max_down_bytes=8388608,max_conns=20,max_rps=10),task_id=task,approved_by='goal-diagnostics',workspace_id=ws)
  tc=TenantContext(tenant_id=owner,user_id='debug',workspace_id=ws)
  lease=await broker.acquire(tc,SandboxSpec(runtime='universal',cpu_millis=1000,memory_mb=512,network='allowlist',workspace_id=ws,idle_timeout=timedelta(minutes=2)),task_id=task,writable=True,busy_for=timedelta(minutes=3))
  assert lease.backend.name=='kata';report['backend_env']=lease.backend_env_id
  print(json.dumps({'event':'lease','tenant':owner,'backend':'kata','env':lease.backend_env_id}),flush=True)
  cloned=await lease.box.exec(lease.env_id,['git','clone','--quiet','https://github.com/octocat/Hello-World.git','repo'],timeout=timedelta(seconds=60))
  assert cloned.exit_code==0, ('clone failed',cloned.exit_code)
  for attempt in range(1):
   try:
    snap=await broker.snapshot(lease)
    report['snapshot_'+str(attempt)]={'passed':True,'files':snap.files,'size':snap.size_bytes}
   except Exception as e:
    report['snapshot_'+str(attempt)]={'passed':False,'code':getattr(e,'code',type(e).__name__)}
   print(json.dumps({'event':'snapshot','attempt':attempt,'result':report['snapshot_'+str(attempt)]}),flush=True)
  assert report['snapshot_0']['passed'], 'write snapshot failed'
  await broker.release(lease,keep=False,skip_snapshot=True)
  await grants.revoke_all(owner)
  readonly=await broker.acquire(tc,SandboxSpec(runtime='universal',cpu_millis=1000,memory_mb=512,network='none',workspace_id=ws,idle_timeout=timedelta(minutes=2)),task_id='',writable=False,busy_for=timedelta(minutes=3))
  assert readonly.backend.name=='kata' and readonly.restored
  content=await readonly.box.read_file(readonly.env_id,'repo/README')
  assert content==b'Hello World!\n'
  async with scope.transaction(read_only=True) as tx:
   binding=await tx.query_one('SELECT task_id FROM sandbox_envs WHERE env_ref=$1',(readonly.env_id,))
   audit=await tx.query('SELECT task_id,state FROM sandbox_grants WHERE task_id=$1',(binding['task_id'],))
   usage=await tx.query('SELECT reservation_id FROM model_usage')
  assert binding['task_id']=='workspace-transfer-'+readonly.env_id
  assert audit and all(row['state']=='revoked' for row in audit)
  assert not usage
  report['readonly_restore']={'passed':True,'internal_task':binding['task_id'],'grants':len(audit),'all_revoked':True,'restored_snapshot':readonly.restored,'README_sha256':hashlib.sha256(content).hexdigest()}
  print(json.dumps({'event':'readonly_restore',**report['readonly_restore']}),flush=True)
  await broker.release(readonly,keep=False,skip_snapshot=True)

 except BaseException as e:
  failure=e;report['failure']={'type':type(e).__name__,'code':getattr(e,'code',None)}
 finally:
  try:
   await grants.revoke_all(owner);await broker.destroy_tenant(owner);report['cleanup']['envs_destroyed']=True
   while True:
    page=await bucket.list(prefix='',limit=1000)
    if not page.items:break
    for obj in page.items:await bucket.delete(obj.key)
   report['cleanup']['objects_removed']=True
   async with scope.transaction() as tx:
    for table in ['sandbox_transfers','workspace_snapshots','workspace_pushes','workspaces','sandbox_grants','sandbox_egress_audit','sandbox_envs','sandbox_usage','sandbox_quota_lock','registry_entries']:
     await tx.execute('DELETE FROM '+table)
   async with store.bind_platform('goal-diagnostics-cleanup').transaction() as tx:await tx.execute("DELETE FROM sandbox_env_directory WHERE owner=$1 AND state IN ('destroyed','failed','lost')",(owner,))
   report['cleanup']['rows_removed']=True
  except BaseException as e:report['cleanup']['failure']={'type':type(e).__name__,'code':getattr(e,'code',None)}
  await broker.aclose();await registry.aclose();await store.aclose();await broker.archiver._store.aclose()
  print(json.dumps(report),flush=True)
 return 1 if failure else 0
asyncio.run(main())
