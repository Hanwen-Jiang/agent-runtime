#!/usr/bin/env python3
"""Manual trusted-bridge verification with real engine subprocess and isolated DB.

Runs under the Worker's Python environment; --engine-python selects the standalone
3.12 engine environment explicitly. Does not claim a production Sandbox image run.
"""
from __future__ import annotations
import argparse,asyncio,dataclasses,hashlib,io,json,os,sqlite3,subprocess,sys,tempfile,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Struct
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'services/py-worker/src'),str(ROOT/'libs/py-core/src'),str(ROOT/'proto/gen/python')]
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.files.activity import FileActivities
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.errors import ProviderError,DeniedError
from agentruntime_core.tenancy import TenantContext

parser=argparse.ArgumentParser();parser.add_argument('--engine-python',required=True);parser.add_argument('--evidence',type=Path,required=True);parser.add_argument('--sandbox-service',action='store_true');args=parser.parse_args()
async def main():
 report={'mode':'explicit_local_engine_real_subprocess','checks':[]}
 with tempfile.TemporaryDirectory(prefix='file-bridge-acceptance-') as tmp:
  tmp=Path(tmp);(tmp/'relational').mkdir();dbpath=tmp/'relational/t1.sqlite3'
  conn=sqlite3.connect(dbpath)
  conn.executescript('''CREATE TABLE runtime_files(file_id TEXT PRIMARY KEY,workspace_id TEXT,name TEXT,created_by TEXT,acl_epoch INTEGER,lifecycle TEXT);
 CREATE TABLE runtime_file_versions(version_id TEXT PRIMARY KEY,file_id TEXT,object_uri TEXT,sha256 TEXT,size_bytes INTEGER,parent_version_id TEXT);
 CREATE TABLE runtime_file_jobs(job_id TEXT PRIMARY KEY,requested_by TEXT,workspace_id TEXT,inputs TEXT,status TEXT,operation TEXT,request TEXT);
 CREATE TABLE runtime_file_grants(file_id TEXT,workspace_id TEXT,subject_type TEXT,subject_id TEXT,permission TEXT);''');conn.commit()
  objects=LocalDiskObjectStoreProvider(tmp/'objects',signing_key=b'synthetic-bridge-key');scope=TenantContext(tenant_id='t1',workspace_id='w1',user_id='u1');store=objects.bind(scope)
  cfg=WorkerConfig(data_dir=tmp,object_store_dir=tmp/'objects');bridge=FileActivities(cfg,objects,None)
  roots=[]
  async def source(name,data,owner='u1'):
   i=len(roots);info=await store.put(f'uploads/src_{i:032x}/{name}',data,content_type='application/octet-stream')
   root={'file_id':f'src_{i:032x}','version_id':f'ver_{i:032x}','acl_epoch':1,'sha256':info.sha256,'object_uri':info.uri,'name':name,'size_bytes':len(data)}
   conn.execute('INSERT INTO runtime_files VALUES(?,?,?,?,?,?)',(root['file_id'],'w1',name,owner,1,'active'))
   conn.execute('INSERT INTO runtime_file_versions VALUES(?,?,?,?,?,?)',(root['version_id'],root['file_id'],info.uri,info.sha256,len(data),''));conn.commit();roots.append(root);return root
  counter=0
  def job(operation,inputs,options=None,selector=None):
   nonlocal counter;counter+=1;jid=f'fjob_{counter:048x}';request={'options':options or {},'selector':selector or {}}
   conn.execute('INSERT INTO runtime_file_jobs VALUES(?,?,?,?,?,?,?)',(jid,'u1','w1',json.dumps(inputs),'running',operation,json.dumps(request)));conn.commit()
   return {'job_id':jid,'tenant_id':'t1','workspace_id':'w1','user_id':'u1','operation':operation,'inputs':inputs,'request':request}
  def passed(name):report['checks'].append({'name':name,'status':'passed'})
  async def denied(label,request,code,using=bridge):
   try:await using.process(request)
   except ProviderError as e:assert e.code==code,(label,e.code)
   else:raise AssertionError(label+' was not rejected')
   passed(label)
  os.environ['AR_FILE_ENGINES_LOCAL_PYTHON']=str(Path(args.engine_python).absolute());os.environ['PYTHONPATH']=str(ROOT/'services/py-worker/src')
  text=await source('notes.txt',b'Real bridge content 42\n')
  request=job('read',[text]);result=await bridge.process(request)
  assert result['status']=='ready' and '42' in result['blocks'][0]['text']
  assert 'path' not in result['artifacts'][0] and result['artifacts'][0]['object_uri'].startswith('obj://t1/file-derived/')
  for artifact in result['artifacts']:
   data=await store.get(store.key_of(artifact['object_uri']));assert hashlib.sha256(data.data).hexdigest()==artifact['sha256'] and data.info.metadata['workspace_id']=='w1'
  passed('source_hash_real_cli_derived_publication')
  callback_job=job('read',[text]);callbacks=[]
  class CompletionHandler(BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_POST(self):
    body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
    assert self.headers['X-Ar-File-Token']=='fixture-completion-token'
    assert self.headers['X-Ar-File-Tenant']=='t1' and self.headers['X-Ar-File-User']=='u1'
    assert self.path.startswith('/v1/file-jobs/'+callback_job['job_id']+'/complete?') and body['result']['status']=='ready'
    callbacks.append(body)
    if len(callbacks)==1:
     self.send_response(503);self.end_headers();return
    with sqlite3.connect(dbpath) as state:
     state.execute("UPDATE runtime_file_jobs SET status='completed' WHERE job_id=?",(callback_job['job_id'],))
    self.send_response(200);self.end_headers();self.wfile.write(b'{}')
  callback_server=ThreadingHTTPServer(('127.0.0.1',0),CompletionHandler)
  callback_thread=threading.Thread(target=callback_server.serve_forever,daemon=True);callback_thread.start()
  os.environ['AR_TASK_SERVICE_URL']=f'http://127.0.0.1:{callback_server.server_port}';os.environ['AR_FILE_INTERNAL_TOKEN']='fixture-completion-token'
  try:
   completed=await bridge.run(json_format.ParseDict(callback_job,Struct()))
   assert json_format.MessageToDict(completed)['status']=='ready' and len(callbacks)==2
   assert conn.execute('SELECT status FROM runtime_file_jobs WHERE job_id=?',(callback_job['job_id'],)).fetchone()[0]=='completed'
   passed('activity_completion_callback_retries_then_persists_without_poll')
  finally:
   callback_server.shutdown();callback_server.server_close();os.environ.pop('AR_TASK_SERVICE_URL',None);os.environ.pop('AR_FILE_INTERNAL_TOKEN',None)

  changed={**request,'operation':'edit'};await denied('operation_snapshot_tamper',changed,'file_inputs_changed')
  await store.put(store.key_of(text['object_uri']),b'corrupted')
  await denied('source_hash_mismatch',job('read',[text]),'file_integrity_mismatch')
  await store.put(store.key_of(text['object_uri']),b'Real bridge content 42\n')
  one=await source('one.csv',b'key,value\na,10\nb,32\n');two=await source('two.csv',b'key,label\na,alpha\nb,beta\n',owner='u2')
  conn.execute('INSERT INTO runtime_file_grants VALUES(?,?,?,?,?)',(two['file_id'],'w1','user','u1','read'));conn.commit()
  request=job('query',[one,two],{'sql':'SELECT SUM(value) AS total FROM data JOIN data_2 USING(key)'})
  result=await bridge.process(request);assert result['status']=='ready',result
  assert '42' in json.dumps(result),result
  passed('multi_root_actual_duckdb_query')
  conn.execute('DELETE FROM runtime_file_grants WHERE file_id=?',(two['file_id'],));conn.commit()
  await denied('secondary_root_revocation',request,'file_access_revoked')
  cancelled=job('read',[text]);conn.execute("UPDATE runtime_file_jobs SET status='cancelled' WHERE job_id=?",(cancelled['job_id'],));conn.commit()
  await denied('cancelled_before_execution',cancelled,'file_job_unavailable')
  class RevokeBeforePublish(FileActivities):
   calls=0
   async def authorize(self,request):
    self.calls+=1
    if self.calls==2:
     conn.execute('UPDATE runtime_files SET acl_epoch=acl_epoch+1 WHERE file_id=?',(text['file_id'],));conn.commit()
    return await super().authorize(request)
  await denied('revoke_during_execution_blocks_publication',job('read',[text]),'file_snapshot_stale',RevokeBeforePublish(cfg,objects,None))
  conn.execute('UPDATE runtime_files SET acl_epoch=1 WHERE file_id=?',(text['file_id'],));conn.commit()
  # Real editing CLI alias produces a candidate; publication remains Go-owned.
  seed=tmp/'seed.docx';subprocess.run([args.engine_python,'-c','from docx import Document;import sys;d=Document();d.add_paragraph("Old text");d.save(sys.argv[1])',str(seed)],check=True)
  doc=await source('draft.docx',seed.read_bytes())
  result=await bridge.process(job('edit',[doc],{'operations':[{'type':'replace_text','paragraph_index':0,'expected_text':'Old text','text':'New text'}]}))
  assert result['status']=='ready' and result['candidate']['object_uri'].startswith('obj://t1/file-derived/') and result['verification']['passed'],result
  passed('m3_edit_alias_candidate_verified_not_published')
  l2=await bridge.process(job('edit',[doc],{'fidelity':'L2','visual_regions':[{'page_index':0,'bbox':[0,0,1,1]}],'operations':[{'type':'replace_text','paragraph_index':0,'expected_text':'Old text','text':'New text'}]}))
  assert l2['status']=='ready' and l2['verification']['passed'] and l2['verification']['fidelity']=='L2' and l2['verification']['visual']['passed'],l2
  passed('m3_l2_independent_visual_verification')
  indexed=await bridge.process(job('index',[text]));assert indexed['status']=='ready';passed('index_operation_reads_real_source')
  await denied('notebook_local_execution_forbidden',job('notebook_execute',[text],{'execute_authorized':True}),'notebook_requires_sandbox')
  original=await store.get(store.key_of(doc['object_uri']));assert original.data==seed.read_bytes();passed('original_bytes_unchanged')
  os.environ['AR_PROFILE']='production'
  await denied('production_local_execution_forbidden',job('read',[text]),'local_file_engine_forbidden')
  os.environ.pop('AR_PROFILE');os.environ.pop('AR_FILE_ENGINES_LOCAL_PYTHON')
  await denied('missing_broker_no_implicit_host_fallback',job('read',[text]),'file_engine_unconfigured')
  if args.sandbox_service:
   sys.path.insert(0,str(ROOT/'services/py-worker'))
   from tests.sandbox_service_support import SandboxService,WORKER_TOKEN
   from agentruntime_worker.sandbox.client import ServiceBroker
   # Explicit local-backend Sandbox Service probe, not an isolation certification.
   # Interpreter's import path is in its own disposable virtual environment.
   library=Path(args.engine_python).absolute().parent.parent/'lib/python3.12/site-packages'
   pth=library/'file_engines_probe.pth';pth.write_text(str(ROOT/'services/py-worker/src')+'\n')
   service=SandboxService(tmp/'sandbox-service',env={'PATH':str(Path(args.engine_python).absolute().parent)+os.pathsep+os.environ.get('PATH','')})
   service.start();broker=ServiceBroker.connect(service.address,WORKER_TOKEN)
   class CheckedBroker:
    async def acquire(self,tenant,spec,**kwargs):
     assert not tenant.workspace_id and not tenant.session_id and not spec.workspace_id
     assert not kwargs['prepare'] and not kwargs['project_environment'] and not kwargs['reuse']
     return await broker.acquire(tenant,spec,**kwargs)
    async def release(self,lease,**kwargs):
     assert kwargs=={'keep':False,'skip_snapshot':True}
     return await broker.release(lease,**kwargs)
   os.environ['AR_FILE_ENGINES_RUNTIME']='python'  # local test backend supports only python
   remote=FileActivities(cfg,objects,CheckedBroker())
   try:
    result=await remote.process(job('read',[text]));assert result['status']=='ready' and '42' in result['blocks'][0]['text'],result
    passed('actual_sandbox_service_no_workspace_no_prepare_no_project_env')
    await remote.db.aclose()
   finally:
    service.stop();pth.unlink(missing_ok=True);os.environ.pop('AR_FILE_ENGINES_RUNTIME',None)
  await bridge.db.aclose();conn.close()
 report['status']='passed';args.evidence.parent.mkdir(parents=True,exist_ok=True);args.evidence.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
asyncio.run(main())
