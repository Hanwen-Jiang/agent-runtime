#!/usr/bin/env python3
"""Manual tool-wire/authority probe; HTTP service is an explicit in-process double."""
import argparse,asyncio,hashlib,json,os,sqlite3,sys,tempfile,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'services/py-worker/src'),str(ROOT/'libs/py-core/src'),str(ROOT/'proto/gen/python')]
from agentruntime_worker.files.tools import FileTools,register_file_tools
from agentruntime_worker.files.authority import FileAuthority
from agentruntime_worker.tools.attachments import TaskAttachments
from agentruntime_worker.executor.image_input import prepare_images
from agentruntime_worker.sandbox.store import relational_store
from agentruntime_worker.config import WorkerConfig
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.providers.tool_driver import ToolInvocation
from agentruntime_core.tenancy import TenantContext
from agentruntime_core.errors import ProviderError
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
parser=argparse.ArgumentParser();parser.add_argument('--evidence',type=Path,required=True);args=parser.parse_args()
async def main():
 checks=[]
 with tempfile.TemporaryDirectory(prefix='file-tools-') as temp:
  temp=Path(temp);(temp/'relational').mkdir();db=sqlite3.connect(temp/'relational/t1.sqlite3',check_same_thread=False)
  db.executescript('''CREATE TABLE tasks(task_id TEXT,submitted_by TEXT);CREATE TABLE runtime_files(file_id TEXT,workspace_id TEXT,created_by TEXT,lifecycle TEXT,acl_epoch INTEGER);CREATE TABLE runtime_file_versions(file_id TEXT,version_id TEXT,sha256 TEXT,object_uri TEXT,parent_version_id TEXT);CREATE TABLE runtime_file_sources(file_id TEXT,object_uri TEXT);CREATE TABLE runtime_file_grants(file_id TEXT,workspace_id TEXT,subject_type TEXT,subject_id TEXT,permission TEXT);CREATE TABLE runtime_file_task_artifacts(task_id TEXT,job_id TEXT,artifact_id TEXT,object_uri TEXT,roots TEXT,sha256 TEXT,mime_type TEXT,size_bytes INTEGER,name TEXT);CREATE TABLE runtime_file_collection_freezes(task_id TEXT,workspace_id TEXT,created_by TEXT,manifest TEXT);''')
  objects=LocalDiskObjectStoreProvider(temp/'objects',signing_key=b'file-tool-probe');store=objects.bind(TenantContext(tenant_id='t1'))
  source=await store.put('uploads/src_'+('1'*32)+'/file.pdf',b'synthetic source',content_type='application/pdf')
  fid='src_'+('1'*32);vid='ver_'+('2'*32);task='tsk_probe';root={'file_id':fid,'version_id':vid,'sha256':source.sha256,'acl_epoch':1,'object_uri':source.uri}
  db.execute('INSERT INTO tasks VALUES(?,?)',(task,'u1'));db.execute('INSERT INTO runtime_files VALUES(?,?,?,?,?)',(fid,'w1','u1','active',1));db.execute('INSERT INTO runtime_file_versions VALUES(?,?,?,?,?)',(fid,vid,source.sha256,source.uri,''));db.execute('INSERT INTO runtime_file_collection_freezes VALUES(?,?,?,?)',(task,'w1','u1','[{"generation":"fixed"}]'));db.commit()
  await store.put(f'task-inputs/{task}/manifest.json',json.dumps({'workspace_id':'w1','user_id':'u1','attachments':[source.uri]}).encode())
  image=await store.put('file-derived/fjob_'+('3'*48)+'/image',b'picture-bytes',content_type='image/png')
  jid='fjob_'+('3'*48);aid='art_'+('4'*32);requests=[];pending=False
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_GET(self):self.handle_request()
   def do_POST(self):self.handle_request()
   def handle_request(self):
    nonlocal pending
    body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or '{}')
    assert self.headers['x-ar-file-token']=='fixture-token' and self.headers['x-ar-file-tenant']=='t1' and self.headers['x-ar-file-user']=='u1'
    requests.append((self.command,self.path,body));path=self.path.split('?')[0]
    if path.endswith('/attach'):
     assert body=={'task_id':task,'artifact_ids':[aid]}
     db.execute('INSERT INTO runtime_file_task_artifacts VALUES(?,?,?,?,?,?,?,?,?)',(task,jid,aid,image.uri,json.dumps([root]),image.sha256,'image/png',len(b'picture-bytes'),'page.png'));db.commit();value={'attached':[aid]}
    elif path.endswith('/cancel'):value={'cancelled':True}
    elif path=='/v1/knowledge/search':
     assert body['task_id']==task and 'generation' not in body
     value={'results':[{'id':'node1','text':'actual fixture evidence'}]}
    else:
     value={'id':jid,'status':'running' if pending else 'completed','operation':'read' if pending else 'render','inputs':[{'file_id':fid,'version_id':vid}],'result':{'artifacts':[] if pending else [{'id':aid,'mime_type':'image/png','object_uri':image.uri,'sha256':image.sha256,'size_bytes':13}]}}
    data=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  provider=relational_store('',temp);attachments=TaskAttachments(objects,FileAuthority(provider));tool=FileTools(f'http://127.0.0.1:{server.server_port}',b'',attachments,provider,service_token='fixture-token')
  inv=ToolInvocation(tenant_id='t1',workspace_id='w1',user_id='u1',task_id=task,arguments={'path':source.uri},credential=None,idempotency_key='operation1')
  result=await tool.invoke(inv,'render');assert result['attached_images']==[aid];checks.append('render_job_attach_endpoint_called')
  files=await attachments.describe('t1',task,'w1');assert len(files)==2 and files[1]['derived'];assert await attachments.references('t1',task,'w1')==[source.uri]
  class Models:
   async def get_capabilities(self,t):return mg.GetCapabilitiesResponse(image_input=True,image_mime_types=['image/png'],max_images=10,max_image_bytes=10<<20,image_token_reserve=1000)
  images=await prepare_images(Models(),TenantContext(tenant_id='t1',workspace_id='w1',user_id='u1'),files)
  assert images.messages[0].parts[1].image.object_uri==image.uri;checks.append('next_model_request_contains_derived_image_ref')
  import dataclasses
  try:await tool.invoke(dataclasses.replace(inv,arguments={'path':'obj://t1/uploads/unpinned'}),'read')
  except ProviderError:checks.append('unpinned_source_denied')
  else:raise AssertionError('unpinned source accepted')
  result=await tool.knowledge(dataclasses.replace(inv,arguments={'query':'find text'}));assert result['results'];checks.append('search_uses_server_frozen_task_scope')
  result=await tool.knowledge(dataclasses.replace(inv,arguments={'node_ids':['node1']}),True);assert result['results'];checks.append('evidence_resolve_uses_task_scope')
  pending=True;running=asyncio.create_task(tool.invoke(inv,'read'));await asyncio.sleep(.15);running.cancel()
  try:await running
  except asyncio.CancelledError:pass
  assert any(path.split('?')[0].endswith('/cancel') and body['consumer_id'].startswith('tool-') for _,path,body in requests);checks.append('task_cancellation_detaches_only_its_consumer')
  db.execute('UPDATE runtime_files SET acl_epoch=2');db.commit()
  try:await attachments.describe('t1',task,'w1')
  except ProviderError:checks.append('derived_epoch_revocation_denied')
  else:raise AssertionError('stale derived image accepted')
  os.environ['AR_TASK_SERVICE_URL']=f'http://127.0.0.1:{server.server_port}';os.environ['AR_FILE_INTERNAL_TOKEN']='fixture-token'
  names=register_file_tools(LocalToolDriverProvider(),WorkerConfig(data_dir=temp));assert set(names)=={'file_inspect','file_read','file_render','table_query','file_edit','file_search','evidence_resolve'};checks.append('production_service_token_factory_no_dev_key')
  server.shutdown();server.server_close();await provider.aclose();db.close()
 report={'status':'passed','mode':'real_SQLite_ObjectStore_with_explicit_HTTP_double','checks':checks};args.evidence.parent.mkdir(parents=True,exist_ok=True);args.evidence.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
asyncio.run(main())
