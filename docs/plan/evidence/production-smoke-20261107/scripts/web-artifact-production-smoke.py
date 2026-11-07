#!/usr/bin/env python3
"""Real deliver_file -> coding-web -> Task API -> ObjectStore download, zero model calls.
Only the new fixture Task row and coding-web TaskRecord projection are seeded;
file creation/upload/registration/list/download/authentication use deployed code.
"""
import argparse,base64,hashlib,importlib.util,json,os,re,secrets,subprocess,time,urllib.request,urllib.error
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--tenant',required=True);p.add_argument('--cookie-file',type=Path,required=True);p.add_argument('--other-cookie-file',type=Path,required=True);p.add_argument('--foreign-cookie-file',type=Path,required=True);p.add_argument('--expected-worker-image',required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
assert re.fullmatch(r'gh-goalaccept-[a-z0-9-]+',a.tenant);assert not a.out.exists()
spec=importlib.util.spec_from_file_location('websmoke',Path(__file__).with_name('web-production-smoke.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
owner=m.Client(a.base,a.cookie_file);other=m.Client(a.base,a.other_cookie_file);foreign=m.Client(a.base,a.foreign_cookie_file)
me=owner.get('/api/me');assert me['tenant']==a.tenant
assert other.get('/api/me')['tenant']==a.tenant
assert foreign.get('/api/me')['tenant']!=a.tenant
alphabet='0123456789ABCDEFGHJKMNPQRSTVWXYZ'
def ulid():
 n=int.from_bytes(secrets.token_bytes(16),'big');s=''
 for _ in range(26):s=alphabet[n&31]+s;n>>=5
 return s
nonce=secrets.token_hex(8);task='tsk_'+ulid();runtime_session='ses_'+ulid();contents=('artifact download acceptance '+nonce+'\n')*64;expected=hashlib.sha256(contents.encode()).hexdigest()
env={**os.environ,'KUBECONFIG':'/etc/rancher/k3s/k3s.yaml'};base=['kubectl','-n','agent-runtime']
d=json.loads(subprocess.check_output([*base,'get','deployment','ar-agent-runtime-worker-program','-o','json'],env=env));assert d['spec']['template']['spec']['containers'][0]['image']==a.expected_worker_image
report={'tenant':a.tenant,'task':task,'runtime_session':runtime_session,'nonce':nonce,'model_calls':0,'checks':{},'cleanup':{},'boundary':'Only task/session association fixture rows are seeded; real deliver_file and authenticated HTTP download chain used'}
project=session=None;fixture_started=False

def pod(action):
 params={'action':action,'tenant':a.tenant,'task':task,'runtime_session':runtime_session,'workspace':session['workspace_id'],'content':contents,'path':'download-'+nonce+'.txt','user':me['user']}
 source='P='+repr(params)+'\n'+r'''
import asyncio,dataclasses,hashlib,json,os,time
from datetime import timedelta
from agentruntime_core.tenancy import TenantContext
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.tools import _relational,context_objects,sandbox_broker,dev_tools
async def main():
 cfg=dataclasses.replace(WorkerConfig.from_env(os.environ),tool_workspace_root=None,sandbox_runtime='universal',sandbox_sessions=True)
 db=_relational(cfg);scope=db.bind(TenantContext(tenant_id=P['tenant']));bucket=context_objects(cfg).bind(TenantContext(tenant_id=P['tenant']));broker=sandbox_broker(cfg);tools=None
 try:
  if P['action']=='prepare':
   async with scope.transaction() as tx:
    await tx.execute("INSERT INTO tasks(task_id,session_id,session_key,client_request_id,status,signaled,last_event_id,created_at,submitted_by,personal_config_pin,personal_config_selection) VALUES ($1,$2,$2,$1,3,1,0,$3,$4,'none','')",(P['task'],P['runtime_session'],int(time.time()*1000),P['user']))
   tenant=TenantContext(tenant_id=P['tenant'],workspace_id=P['workspace'],user_id=P['user'],session_id=P['runtime_session'])
   lease=await broker.acquire(tenant,SandboxSpec(runtime='universal',workspace_id=P['workspace'],idle_timeout=timedelta(minutes=3)),task_id=P['task'],writable=True,reuse=True,busy_for=timedelta(minutes=2))
   await lease.box.write_file(lease.env_id,P['path'],P['content'].encode());await broker.release(lease,keep=True)
   tools=dev_tools(cfg);artifact=(await tools.bind(tenant).invoke('deliver_file','v1',{'path':P['path']},idempotency_key='web-artifact-'+P['task'],task_id=P['task'])).output
   await broker.release(lease,keep=False,skip_snapshot=True)
   print(json.dumps({'artifact':artifact,'backend':lease.backend.name,'env_destroyed':True}))
  elif P['action']=='cleanup':
   await broker.destroy_workspace(P['tenant'],P['workspace'])
   async with scope.transaction(read_only=True) as tx:rows=await tx.query('SELECT object_uri FROM task_artifacts WHERE task_id=$1',(P['task'],))
   for row in rows:await bucket.delete(bucket.key_of(row['object_uri']))
   async with scope.transaction() as tx:
    for table in ['task_artifacts','task_artifact_reservations','task_artifact_quota_lock','sandbox_transfers','tasks']:
     await tx.execute('DELETE FROM '+table+' WHERE task_id=$1',(P['task'],))
    live=await tx.query("SELECT env_ref FROM sandbox_envs WHERE workspace_id=$1 AND state IN ('ready','creating','starting','destroying')",(P['workspace'],))
    assert not live
    assert not await tx.query('SELECT reservation_id FROM model_usage WHERE task_id=$1',(P['task'],))
   print(json.dumps({'artifact_objects_deleted':len(rows),'task_rows_removed':True,'envs_destroyed':True,'model_usage_rows':0}))
 finally:
  await broker.aclose();await db.aclose();await broker.archiver._store.aclose()
  if tools:
   stores={}
   for group in tools._tools.values():
    for tool in group.values():
     store=getattr(tool.handler,'store',None)
     if store is not None and hasattr(store,'aclose'):stores[id(store)]=store
   for store in stores.values():await store.aclose()
asyncio.run(main())
'''
 result=subprocess.run([*base,'exec','-i','deployment/ar-agent-runtime-worker-program','--','python','-'],input=source.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,timeout=300)
 if result.returncode:
  raise RuntimeError('fixture '+action+' failed; exit '+str(result.returncode))
 return json.loads(result.stdout.decode().strip().splitlines()[-1])

def request_bytes(client,path):
 req=urllib.request.Request(client.base+path,headers={'X-Coding-Web':'1','User-Agent':'agentruntime-goal-acceptance/1.0'})
 try:r=client.opener.open(req,timeout=30)
 except urllib.error.HTTPError as e:r=e
 with r:return r.status,r.read(),dict(r.headers)

try:
 status,project,op,_=owner.mutation('POST','/api/projects',{'name':'Artifact download acceptance '+nonce,'layout':'WORKSPACE_LAYOUT_MULTI'})
 assert status==201,(status,project)
 pbase='/api/projects/'+project['id']
 status,session,_,_=owner.mutation('POST',pbase+'/sessions',{'title':'Zero-model artifact fixture'})
 assert status==201,(status,session)
 fixture_started=True;prepared=pod('prepare');artifact=prepared['artifact'];assert artifact['sha256']==expected
 sbase=pbase+'/sessions/'+session['id'];report.update(project_id=project['id'],session_id=session['id'],workspace_id=session['workspace_id'],artifact=artifact,backend=prepared['backend'])
 projection={'id':task,'message':'Zero-model acceptance fixture; not submitted','created_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'created_by':me['user'],'status':'TASK_STATUS_DONE','settled':True}
 target='/data/'+a.tenant+'/tasks-'+session['workspace_id']+'.json'
 # New unique task projection only. Never overwrite a pre-existing session file.
 shell='set -eu; umask 077; test ! -e "$1"; set -C; cat > "$1"'
 subprocess.run([*base,'exec','-i','deployment/coding-web','--','sh','-c',shell,'fixture-projection',target],input=json.dumps([projection]).encode(),check=True,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
 items=owner.get(sbase+'/tasks/'+task+'/artifacts')['artifacts'];report['checks']['list']=items;assert len(items)==1,('artifact count',len(items));item=items[0]
 assert item['sha256']==expected and item['size_bytes']==len(contents.encode()),('listed metadata',item)
 path=item['url'];status,body,headers=request_bytes(owner,path)
 normalized={k.lower():v for k,v in headers.items()}
 report['checks']['download']={'http_status':status,'sha256':hashlib.sha256(body).hexdigest(),'bytes':len(body),'headers':{k:normalized.get(k) for k in ['content-type','content-length','content-disposition','cache-control','x-content-type-options','content-security-policy']}}
 assert status==200 and hashlib.sha256(body).hexdigest()==expected,('download mismatch',report['checks']['download'])
 assert normalized.get('cache-control')=='no-store' and normalized.get('x-content-type-options')=='nosniff' and normalized.get('content-security-policy')=='sandbox',('security headers',report['checks']['download'])
 assert 'attachment' in normalized.get('content-disposition',''),('disposition',report['checks']['download'])
 other_status,other_body,_=request_bytes(other,path)
 assert other_status==200 and hashlib.sha256(other_body).hexdigest()==expected,'tenant-shared session is legitimately visible to another user'
 foreign_status,_,_=request_bytes(foreign,path);assert foreign_status==404,foreign_status
 wrong_task='tsk_'+ulid();wrong_status,_,_=request_bytes(owner,path.replace(task,wrong_task));assert wrong_status==404,wrong_status
 report['checks'].update(delivery_registered=True,coding_web_download_status=status,sha256=expected,bytes=len(body),security_headers=report['checks']['download']['headers'],same_tenant_shared_read=other_status,cross_tenant_denied=foreign_status,wrong_task_denied=wrong_status)
 report['passed']=True
except BaseException as exc:
 report['passed']=False;report['failure']={'type':type(exc).__name__,'message':str(exc)[:400]}
finally:
 if fixture_started:
  try:report['cleanup']['runtime']=pod('cleanup')
  except BaseException as exc:report['cleanup']['runtime_error']=type(exc).__name__;report['passed']=False
 if project:
  try:
   status,_,operation,_=owner.mutation('DELETE','/api/projects/'+project['id'],{})
   report['cleanup']['project_status']=status;report['cleanup']['project_operation']=operation
   if status!=200:report['passed']=False
  except BaseException as exc:report['cleanup']['project_error']=type(exc).__name__;report['passed']=False
 a.out.parent.mkdir(parents=True,exist_ok=True);a.out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');a.out.chmod(0o600)
 print(json.dumps({'passed':report.get('passed'),'evidence':str(a.out),'cleanup':report['cleanup'],'model_calls':0},ensure_ascii=False))
raise SystemExit(0 if report.get('passed') else 1)
