#!/usr/bin/env python3
"""Real isolated Temporal/File API/Sandbox Service lifecycle acceptance.

Faults below are explicit admission gates or transient exceptions around the
production FileActivities, not substitutes for parsing or workflow execution.
Run only in a disposable task directory. No production endpoint is accepted.
"""
from __future__ import annotations
import argparse,asyncio,contextlib,hashlib,json,os,signal,socket,sqlite3,subprocess,sys,time,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'services/py-worker/src'),str(ROOT/'libs/py-core/src'),str(ROOT/'proto/gen/python'),str(ROOT/'services/py-worker')]
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Struct
from temporalio import activity,workflow
from temporalio.client import Client
from temporalio.worker import Worker,Replayer
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
from temporalio.worker import UnsandboxedWorkflowRunner
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from datetime import timedelta
from agentruntime_worker.files.activity import FileActivities
from agentruntime_worker.workflows.files import FileOperationWorkflow
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.executor.dev import dev_object_store,data_root
from agentruntime_worker.worker import connect
from agentruntime_worker.sandbox.client import ServiceBroker
from agentruntime_core.errors import TransientError

@workflow.defn(name='FileOperationWorkflow')
class LegacyFileOperationWorkflow:
 @workflow.run
 async def run(self,request:Struct)->Struct:
  try:
   return await workflow.execute_activity('file_operation',request,result_type=Struct,task_queue='workspace',start_to_close_timeout=timedelta(minutes=15),heartbeat_timeout=timedelta(seconds=45),retry_policy=RetryPolicy(maximum_attempts=2,initial_interval=timedelta(seconds=3)))
  except ActivityError:
   result=Struct();result.update({'status':'failed','error':{'code':'file_operation_failed','message':'文件处理失败或超时，可重新开始处理'}});return result

class InstrumentedActivities(FileActivities):
 def __init__(self,config,objects,broker,state):super().__init__(config,objects,broker);self.state=state
 @activity.defn(name='file_operation')
 async def run(self,message:Struct)->Struct:
  req=json_format.MessageToDict(message);job=req['job_id'];attempt=activity.info().attempt
  scenario=req['inputs'][0]['name'].removesuffix('.txt')
  with (self.state/'attempts.jsonl').open('a') as f:f.write(json.dumps({'job':job,'scenario':scenario,'attempt':attempt,'pid':os.getpid()})+'\n')
  if scenario=='timeout':await asyncio.sleep(120) # Actual server-enforced heartbeat timeout, twice.
  if scenario=='failure' or scenario=='retry' and attempt==1:raise TransientError('acceptance_fault','controlled transport failure')
  if scenario in ('recover','cancel','shared'):
   while not (self.state/('release-'+job)).exists():
    activity.heartbeat({'controlled_gate':True});await asyncio.sleep(.2)
  return await super().run(message)


from agentruntime_worker.activities.types import ToolLoopStep,ToolLoopStepInput,ToolCallInput,Tenant,StagePolicy
from agentruntime_worker.workflows.stage_runner import SubAgentWorkflow,SubAgentInput
from agentruntime_worker import names

class ScopeScript:
 @activity.defn(name=names.TOOL_LOOP_STEP)
 async def step(self,inp:ToolLoopStepInput)->ToolLoopStep:
  if inp.iteration==1:
   requested=inp.node['inputs']['probe_call']
   return ToolLoopStep(action='tool',tool=requested['tool'],args=requested['args'])
  return ToolLoopStep(action='finish',result=inp.history[-1])

@workflow.defn(name='ParentToolProbeWorkflow')
class ParentToolProbeWorkflow:
 @workflow.run
 async def run(self,inp:ToolCallInput)->dict:
  return await workflow.execute_activity(names.CALL_TOOL,inp,task_queue='program',result_type=dict,start_to_close_timeout=timedelta(minutes=2))

@workflow.defn(name='SubAgentWorkflow')
class LegacyChildProbeWorkflow:
 @workflow.run
 async def run(self,inp:ToolCallInput)->dict:
  from dataclasses import asdict
  old=asdict(inp);old.pop('isolated');old.pop('file_scopes')
  try:return await workflow.execute_activity(names.CALL_TOOL,old,task_queue='program',result_type=dict,start_to_close_timeout=timedelta(minutes=2),retry_policy=RetryPolicy(maximum_attempts=1))
  except ActivityError:return {'denied':True}

async def worker_main(state,legacy=False):
 config=WorkerConfig.from_env(os.environ);objects=dev_object_store(config,data_root(config));client=await connect(config)
 broker=ServiceBroker.connect(os.environ['AR_SANDBOX_SERVICE_ADDRESS'],os.environ['FI_SANDBOX_WORKER_TOKEN'])
 acts=InstrumentedActivities(config,objects,broker,state)
 try:
  async with contextlib.AsyncExitStack() as stack:
   await stack.enter_async_context(Worker(client,task_queue='workspace',workflows=[LegacyFileOperationWorkflow if legacy else FileOperationWorkflow],activities=list(acts.activities().values()),workflow_runner=UnsandboxedWorkflowRunner() if legacy else SandboxedWorkflowRunner(),graceful_shutdown_timeout=timedelta(seconds=2)))
   if not legacy:
    from tests.executor_harness import make_fixture
    from agentruntime_worker.files.tools import register_file_tools
    from agentruntime_core.enums import Effect
    fixture=make_fixture(state/'executor-fixture');fixture.executor.deps.objects=objects
    fixture.register('echo',Effect.READ)
    register_file_tools(fixture.tools,config)
    await stack.enter_async_context(Worker(client,task_queue='program',activities=[fixture.executor.call_tool]))
    await stack.enter_async_context(Worker(client,task_queue='model',activities=[ScopeScript().step]))
    await stack.enter_async_context(Worker(client,task_queue='fi-scope',workflows=[SubAgentWorkflow,ParentToolProbeWorkflow],workflow_runner=UnsandboxedWorkflowRunner()))
    await stack.enter_async_context(Worker(client,task_queue='fi-legacy-child',workflows=[LegacyChildProbeWorkflow],workflow_runner=UnsandboxedWorkflowRunner()))
   (state/'worker-ready').write_text(str(os.getpid()));await asyncio.Event().wait()
 finally:await broker.aclose()

def freeport():
 with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
async def wait(fn,label,timeout=160):
 end=time.monotonic()+timeout
 while time.monotonic()<end:
  try:
   value=fn()
   if value:return value
  except (OSError,sqlite3.Error,urllib.error.URLError):pass
  await asyncio.sleep(.2)
 raise RuntimeError('timeout waiting for '+label)

async def acceptance(state,evidence,scopes_only=False):
 state.mkdir(parents=True,exist_ok=False)
 report={'checks':[],'temporal':subprocess.check_output(['temporal','--version'],text=True).strip(),'mode':'real_temporal_go_file_api_sandbox_service_local_backend','namespace':'fi-'+str(int(time.time()))}
 namespace=report['namespace'];port=freeport();apiport=freeport();processes=[];worker=None;service=None
 def spawn(argv,env,name):
  log=(state/(name+'.log')).open('ab');p=subprocess.Popen(argv,cwd=ROOT,env=env,stdout=log,stderr=log,start_new_session=True);log.close();processes.append(p);return p
 def stop(p,kill=False):
  if p and p.poll() is None:
   os.killpg(p.pid,signal.SIGKILL if kill else signal.SIGTERM)
   try:p.wait(timeout=10)
   except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
 def check(label):report['checks'].append({'name':label,'status':'passed'});print('PASS '+label,flush=True)
 try:
  env={**os.environ};temporal=spawn(['temporal','server','start-dev','--ip','127.0.0.1','--port',str(port),'--headless','--namespace',namespace,'--db-filename',str(state/'temporal.sqlite')],env,'temporal')
  await wait(lambda:socket.create_connection(('127.0.0.1',port),.2).close() or True,'Temporal server',60)
  bin_dir=state/'bin';bin_dir.mkdir();subprocess.run(['go','build','-o',str(bin_dir/'task-service'),'./services/go-task-service/cmd/task-service'],cwd=ROOT,check=True)
  from tests.sandbox_service_support import SandboxService,WORKER_TOKEN
  service=SandboxService(state/'sandbox-service',bin_dir=bin_dir/'sandbox',env={'PATH':str(Path(sys.executable).parent)+os.pathsep+os.environ.get('PATH','')});service.start()
  # The disposable venv sees the standalone engine without mutable host state.
  engine_site=Path(sys.prefix)/f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
  (engine_site/'fi-temporal-engine.pth').write_text(str(ROOT/'services/py-worker/src')+'\n')
  data=state/'data';data.mkdir();objects=state/'objects';objects.mkdir()
  env.update({'AR_TEMPORAL_ADDRESS':f'127.0.0.1:{port}','AR_TEMPORAL_NAMESPACE':namespace,'AR_HTTP_ADDR':f'127.0.0.1:{apiport}','AR_DATA_DIR':str(data),'AR_OBJECT_STORE_DIR':str(objects),'AR_SECRETS_FILE':str(state/'secrets.json'),'AR_DEV_SIGNING_KEY':'temporal-acceptance-only-key-0123456789','AR_DEV_RATE_LIMIT_PER_MINUTE':'10000','AR_FILE_INTERNAL_TOKEN':'file-acceptance-only-token','AR_TASK_SERVICE_URL':f'http://127.0.0.1:{apiport}','AR_FILE_ENGINES_RUNTIME':'python','AR_SANDBOX_SERVICE_ADDRESS':service.address,'FI_SANDBOX_WORKER_TOKEN':WORKER_TOKEN,'PYTHONPATH':os.pathsep.join(sys.path[:3]),'AR_CLAIM_CHECK_THRESHOLD_BYTES':str(1<<20)})
  for key in ('AR_RELATIONAL_DSN','AR_VALKEY_URL','AR_PROFILE','AR_FILE_ENGINES_LOCAL_PYTHON','AR_REGISTRY_ADDR','AR_INTERNAL_ADDR'):env.pop(key,None)
  api=spawn([str(bin_dir/'task-service'),'serve'],env,'task-api')
  await wait(lambda:urllib.request.urlopen(f'http://127.0.0.1:{apiport}/healthz',timeout=.3).status==200,'File API',60)
  def http(method,path,body=None,raw=None):
   payload=raw if raw is not None else json.dumps(body).encode() if body is not None else None
   req=urllib.request.Request(env['AR_TASK_SERVICE_URL']+path+('?' if '?' not in path else '&')+'workspace_id=w1',data=payload,method=method,headers={'Content-Type':'application/json','X-Ar-File-Token':env['AR_FILE_INTERNAL_TOKEN'],'X-Ar-File-Tenant':'t1','X-Ar-File-User':'u1'})
   with urllib.request.urlopen(req,timeout=10) as res:return json.load(res)
  def row(jid):
   with sqlite3.connect(data/'relational/t1.sqlite3') as db:db.row_factory=sqlite3.Row;return dict(db.execute('SELECT * FROM runtime_file_jobs WHERE job_id=?',(jid,)).fetchone())
  def terminal(jid,wanted):
   current=row(jid)
   if current['status'] in ('completed','failed','cancelled') and current['status']!=wanted:
    raise RuntimeError('unexpected terminal '+current['status']+': '+current['result'])
   return current['status']==wanted
  def attempts(jid):
   f=state/'attempts.jsonl';return [json.loads(line) for line in f.read_text().splitlines() if json.loads(line)['job']==jid] if f.exists() else []
  def start(scenario,consumer):
   source=http('POST','/v1/uploads?filename='+scenario+'.txt',raw=('Real '+scenario+' content 42\n').encode())
   return http('POST','/v1/file-jobs',{'operation':'read','request_id':consumer,'inputs':[{'file_id':source['file_id'],'version_id':source['version_id']}],'options':{},'selector':{}})
  def start_worker(legacy=False):
   (state/'worker-ready').unlink(missing_ok=True)
   return spawn([sys.executable,str(Path(__file__).resolve()),'--worker','--state',str(state),*(['--legacy'] if legacy else [])],env,'worker-legacy' if legacy else 'worker')
  # A genuine old-version history is recorded before loading the new code.
  worker=start_worker(True);await wait(lambda:(state/'worker-ready').exists(),'legacy worker')
  old=start('legacy','legacy-request');await wait(lambda:terminal(old['id'],'completed'),'legacy terminal')
  stop(worker);worker=None
  client=await Client.connect(env['AR_TEMPORAL_ADDRESS'],namespace=namespace)
  old_history=await client.get_workflow_handle('fileop-'+old['id']).fetch_history();(state/'legacy-history.json').write_text(old_history.to_json())
  replay=await Replayer(workflows=[FileOperationWorkflow]).replay_workflow(old_history,raise_on_replay_failure=False);assert replay.replay_failure is None,replay.replay_failure
  check('unmodified_legacy_history_replays_with_terminal_patch')
  worker=start_worker();await wait(lambda:(state/'worker-ready').exists(),'worker')
  simple=start('simple','simple-idempotent');before=row(simple['id'])['inputs']
  repeated=start('simple','simple-idempotent');assert repeated['id']==simple['id']
  await wait(lambda:terminal(simple['id'],'completed'),'no-poll terminal');assert row(simple['id'])['inputs']==before
  assert len(attempts(simple['id']))==1
  check('real_api_idempotency_frozen_inputs_and_completion_without_get')
  if not scopes_only:
   recover=start('recover','recover-request');await wait(lambda:attempts(recover['id']),'started recovery activity')
   stop(worker,True);worker=None;(state/('release-'+recover['id'])).touch();worker=start_worker();await wait(lambda:(state/'worker-ready').exists(),'replacement worker')
   await wait(lambda:terminal(recover['id'],'completed'),'recovery completion',130)
   assert [a['attempt'] for a in attempts(recover['id'])]==[1,2]
   check('worker_sigkill_heartbeat_timeout_retry_and_recovery')
   shared=start('shared','consumer-a');other=start('shared','consumer-b');assert other['id']==shared['id'];await wait(lambda:attempts(shared['id']),'shared started')
   cancelled=http('POST','/v1/file-jobs/'+shared['id']+'/cancel',{'consumer_id':'consumer-a'});assert not cancelled['job_cancelled'];assert row(shared['id'])['status']=='running'
   (state/('release-'+shared['id'])).touch();await wait(lambda:terminal(shared['id'],'completed'),'remaining consumer')
   check('one_consumer_cancel_keeps_shared_job_running')
   cancel=start('cancel','cancel-consumer');await wait(lambda:attempts(cancel['id']),'cancel started')
   cancelled=http('POST','/v1/file-jobs/'+cancel['id']+'/cancel',{'consumer_id':'cancel-consumer'});assert cancelled['job_cancelled']
   await wait(lambda:terminal(cancel['id'],'cancelled'),'HTTP cancellation terminal');(state/('release-'+cancel['id'])).touch()
   await asyncio.sleep(2);assert row(cancel['id'])['status']=='cancelled'
   check('last_consumer_cancel_persists_terminal_and_blocks_late_result')
   retry=start('retry','retry-one');failure=start('failure','failure-twice');timeout=start('timeout','timeout-twice')
   await wait(lambda:terminal(retry['id'],'completed'),'transient retry')
   await wait(lambda:terminal(failure['id'],'failed'),'retry exhausted terminal')
   await wait(lambda:terminal(timeout['id'],'failed'),'actual heartbeat timeouts terminal',160)
   assert [a['attempt'] for a in attempts(retry['id'])]==[1,2]
   assert [a['attempt'] for a in attempts(failure['id'])]==[1,2]
   assert [a['attempt'] for a in attempts(timeout['id'])]==[1,2]
   check('transient_failure_and_actual_timeout_retry_at_most_twice_then_persist')
   for name,j in [('recovered',recover),('failed',failure),('timeout',timeout),('cancelled',cancel)]:
    history=await client.get_workflow_handle('fileop-'+j['id']).fetch_history();(state/(name+'-history.json')).write_text(history.to_json())
    if name!='cancelled':
     replay=await Replayer(workflows=[FileOperationWorkflow]).replay_workflow(history,raise_on_replay_failure=False);assert replay.replay_failure is None,replay.replay_failure
   check('real_recovery_failure_and_timeout_histories_replay')
  if scopes_only:
   from openpyxl import Workbook
   import io,base64,hmac
   workbook=Workbook();public=workbook.active;public.title='Public';public.append(['label','value']);public.append(['PUBLIC_ONLY',42]);secret=workbook.create_sheet('Secret');secret.append(['SECRET_MARKER_DO_NOT_LEAK',987654]);raw=io.BytesIO();workbook.save(raw)
   source=http('POST','/v1/uploads?filename=two-sheets.xlsx',raw=raw.getvalue());uri=source['uri']
   # Seed the same server-side task authority rows/manifests needed by FileTools;
   # a paid model/TaskWorkflow is intentionally not started by this fixture.
   task='tsk_scope';empty_task='tsk_empty_scope';collection_task='tsk_collection_scope'
   with sqlite3.connect(data/'relational/t1.sqlite3') as db:
    for tid in (task,empty_task,collection_task):
     db.execute("INSERT INTO tasks(task_id,session_id,session_key,client_request_id,status,signaled,last_event_id,created_at,outcome,submitted_by,message) VALUES(?,?,?,?,0,1,0,0,'','u1','scope probe')",(tid,'ses_01HZY2J5K3M4N5P6Q7R8S9T0VW','ses_01HZY2J5K3M4N5P6Q7R8S9T0VW',tid))
   from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
   from agentruntime_core.tenancy import TenantContext
   object_store=LocalDiskObjectStoreProvider(objects,signing_key=b'acceptance')
   scoped=object_store.bind(TenantContext(tenant_id='t1'))
   await scoped.put('task-inputs/'+task+'/manifest.json',json.dumps({'user_id':'u1','workspace_id':'w1','attachments':[uri]}).encode())
   await scoped.put('task-inputs/'+collection_task+'/manifest.json',json.dumps({'user_id':'u1','workspace_id':'w1','attachments':[],'collections':[{'id':'fixture-collection','generation':'fixed'}]}).encode())
   fake_sheet=http('POST','/v1/uploads?filename=fake.xlsx',raw=b'RAW_SECRET_MUST_NOT_BE_RETURNED')
   fake_uri=fake_sheet['uri']
   await scoped.put('task-inputs/'+task+'/manifest.json',json.dumps({'user_id':'u1','workspace_id':'w1','attachments':[uri,fake_uri]}).encode())
   delegation=[{'path':uri,'selector':{'sheet':'Public'}}]
   tenant=Tenant('t1','u1','w1','ses_01HZY2J5K3M4N5P6Q7R8S9T0VW')
   async def child(label,tool,arguments,scopes=delegation,task_id=task):
    inputs={'probe_call':{'tool':tool,'args':arguments}}
    if scopes is not None:inputs['file_scopes']=scopes
    inp=SubAgentInput(tenant,task_id,1,{'id':label,'kind':'goal','mode':'sub_agent','goal':'Read the explicitly delegated sheet','done_when':'Return actual evidence','inputs':inputs,'tools':[tool],'max_iterations':2},'read',{tool:'read'},StagePolicy())
    handle=await client.start_workflow(SubAgentWorkflow.run,inp,id='scope-'+label,task_queue='fi-scope')
    result=await asyncio.wait_for(handle.result(),90)
    history=await handle.fetch_history();(state/('scope-'+label+'-history.json')).write_text(history.to_json())
    replay=await Replayer(workflows=[SubAgentWorkflow]).replay_workflow(history,raise_on_replay_failure=False);assert replay.replay_failure is None,replay.replay_failure
    return result.result
   allowed=await child('public','file_read',{'path':uri,'selector':{'sheet':'Public'}})
   rendered=json.dumps(allowed)
   assert 'PUBLIC_ONLY' in rendered and 'SECRET_MARKER' not in rendered and '"name": "Secret"' not in rendered,allowed
   check('real_subagent_public_sheet_read_no_secret_outline_or_content')
   query=await child('query','table_query',{'path':uri,'selector':{'sheet':'Public'},'sql':'SELECT SUM(value) AS total FROM data'})
   assert '42' in json.dumps(query) and 'SECRET_MARKER' not in json.dumps(query),query
   check('real_subagent_query_only_materializes_delegated_sheet')
   denied_cases=[('secret','file_read',{'path':uri,'selector':{'sheet':'Secret'}},delegation),('sheet-alias','file_read',{'path':uri,'selector':{'sheet':'Public','sheet_id':'sheet-1'}},delegation),('range-escape','file_read',{'path':uri,'selector':{'sheet':'Public','range':'Secret!A1:B2'}},delegation),('extra-source','table_query',{'path':uri,'selector':{'sheet':'Public'},'options':{'tables':[{'path':uri,'selector':{'sheet':'Secret'}}]}},delegation),('raw','context_read',{'path':uri},delegation),('shell','shell_run',{'argv':['cat','two-sheets.xlsx']},delegation),('missing','file_read',{'path':uri,'selector':{'sheet':'Public'}},None)]
   denied_cases.extend([('other-file','file_read',{'path':fake_uri,'selector':{'sheet':'Public'}},delegation),('fake-sheet-format','file_read',{'path':fake_uri,'selector':{'sheet':'Public'}},[{'path':fake_uri,'selector':{'sheet':'Public'}}])])
   for label,tool,arguments,scopes in denied_cases:
    response=await child(label,tool,arguments,scopes);assert 'error' in response and 'observation' not in response,(label,response)
   check('secret_raw_shell_alias_range_extra_sources_and_no_scope_denied')
   legacy=await client.execute_workflow(LegacyChildProbeWorkflow.run,ToolCallInput(tenant,task,1,'legacycall','context_read',{'path':uri},'read'),id='legacy-child-no-scope',task_queue='fi-legacy-child')
   assert legacy['denied'];check('legacy_pending_child_without_scope_is_denied_from_trusted_workflow_type')
   parent=await client.execute_workflow(ParentToolProbeWorkflow.run,ToolCallInput(tenant,task,1,'parentread','file_read',{'path':uri,'selector':{'sheet':'Secret'}},'read'),id='parent-unrestricted',task_queue='fi-scope')
   assert 'SECRET_MARKER' in json.dumps(parent);check('normal_parent_keeps_its_existing_file_access')
   ordinary=await child('ordinary','echo',{'message':'normal-coding-agent'},None,empty_task)
   assert ordinary['observation']['echo']['message']=='normal-coding-agent';check('no_attachment_coding_subagent_retains_original_tools')
   denied_collection=await child('collection-only','echo',{'message':'must not inherit collection'},None,collection_task)
   assert 'error' in denied_collection;check('collection_only_parent_fail_closed_without_delegation')
   public_result,parent_result=await asyncio.gather(child('parallel-public','file_read',{'path':uri,'selector':{'sheet':'Public'}}),client.execute_workflow(ParentToolProbeWorkflow.run,ToolCallInput(tenant,task,1,'parallelparent','file_read',{'path':uri,'selector':{'sheet':'Secret'}},'read'),id='parallel-parent',task_queue='fi-scope'))
   assert 'SECRET_MARKER' not in json.dumps(public_result) and 'SECRET_MARKER' in json.dumps(parent_result)
   check('concurrent_parent_child_context_scopes_do_not_bleed')
  report['status']='passed';report['namespace']=namespace;report['attempts']=[json.loads(line) for line in (state/'attempts.jsonl').read_text().splitlines()]
 finally:
  stop(worker)
  if service:service.stop()
  for p in reversed(processes):stop(p)
  evidence.parent.mkdir(parents=True,exist_ok=True);evidence.write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps(report),flush=True)

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--state',type=Path,required=True);parser.add_argument('--evidence',type=Path);parser.add_argument('--worker',action='store_true');parser.add_argument('--legacy',action='store_true');parser.add_argument('--scopes-only',action='store_true');args=parser.parse_args()
 if args.worker:asyncio.run(worker_main(args.state,args.legacy))
 else:
  if args.evidence is None:parser.error('--evidence required')
  asyncio.run(acceptance(args.state.resolve(),args.evidence.resolve(),args.scopes_only))
