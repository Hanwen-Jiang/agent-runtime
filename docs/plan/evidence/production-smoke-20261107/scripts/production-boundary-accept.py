"""Zero-model fixture-task acceptance of deployed HOME pinning and egress grants.
The owner is fresh and generated here. No SubmitTask, workflow, model, Git push,
configuration rollout or shared environment mutation is performed. The fixture
rows exercise Runtime activity/service boundaries, not Task API admission.
"""
import asyncio, dataclasses, hashlib, json, os, secrets, time
from datetime import timedelta
from agentruntime.registry.v1 import registry_pb2
from agentruntime_core.tenancy import TenantContext
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.registry.client import GrpcRegistry
from agentruntime_worker.sandbox.grants import GrantStore, NetworkPolicy, check_request
from agentruntime_worker.sandbox.times import iso, utc_now
from agentruntime_worker.tools import sandbox_broker, context_objects, _relational, _platform_secret

async def main():
    assert os.environ.get('AR_TRANSFER_ACCEPT_EXECUTE')=='1'
    nonce=secrets.token_hex(8);owner='transfer-accept-boundary-'+nonce
    cfg=dataclasses.replace(WorkerConfig.from_env(os.environ),tool_workspace_root=None,sandbox_sessions=True,sandbox_runtime='universal')
    broker=sandbox_broker(cfg);db=_relational(cfg);scope=db.bind(TenantContext(tenant_id=owner));bucket=context_objects(cfg).bind(TenantContext(tenant_id=owner))
    token=await _platform_secret(cfg,'registry/publish-token');assert token
    registry=GrpcRegistry(cfg.registry_address,publish_token=token.decode())
    grants=GrantStore(db)
    report={'tenant':owner,'started_unix':time.time(),'model_calls':0,'model_cost_usd':0,'checks':{},'cleanup':{},'owned_cache_blobs':{},'boundary':'Fixture task rows and authorized internal GrantStore; public PersonalConfig HTTP covered separately; no Task API Submit'}
    print(json.dumps({'event':'acceptance_started','tenant':owner,'started_unix':report['started_unix']}),flush=True)
    async def query(sql,*args):
        async with scope.transaction(read_only=True) as tx:return await tx.query(sql,args)
    async def sql(statement,*args):
        async with scope.transaction() as tx:return await tx.execute(statement,args)
    async def acquire(task,session='',reuse=False):
        tenant=TenantContext(tenant_id=owner,user_id='acceptance',session_id=session)
        lease=await broker.acquire(tenant,SandboxSpec(runtime='universal',cpu_millis=1000,memory_mb=512,idle_timeout=timedelta(minutes=3)),task_id=task,writable=False,reuse=reuse,busy_for=timedelta(minutes=2))
        assert lease.backend.kind=='firecracker'
        return lease
    async def py(lease,code,*args):
        out=await lease.box.exec(lease.env_id,['python3','-c',code,*args],timeout=timedelta(seconds=30))
        assert out.exit_code==0,('sandbox command failed',out.exit_code)
        return json.loads(out.stdout)
    curl="""import json,os,subprocess,sys,tempfile
fd,path=tempfile.mkstemp();os.close(fd)
try:
 p=subprocess.run(['curl','-sS','--noproxy','','--max-time','15','--max-redirs','0','-o',path,'-w','%{http_code}',sys.argv[1]],capture_output=True)
 body=open(path,'rb').read(2048).decode(errors='replace')
 print(json.dumps({'exit':p.returncode,'status':p.stdout.decode(),'gateway_denied':'出网网关拒绝' in body,'private_literal_denied':'public_ip_literal' in body,'quota_denied':'bytes_exceeded' in body,'body_bytes':os.stat(path).st_size}))
finally:os.unlink(path)
"""
    async def request(lease,target):return await py(lease,curl,target)
    async def wait_denied(lease,target):
        for _ in range(6):
            result=await request(lease,target)
            if result['status']=='403':return result
            await asyncio.sleep(2)
        raise AssertionError(('grant still usable',result))
    error=None
    try:
        net={'allowed':True,'modes':['deny','allowlist','open'],'hosts':[],'max_grant_minutes':15,'approver':'submitter','max_up_bytes':1048576,'max_down_bytes':1048576,'max_conns':2,'max_rps':5}
        policy={'model_allowlist':[],'model_budget':{'daily_usd':0,'monthly_usd':0},'sandbox':{'backend':'firecracker','fallback':[],'min_isolation':'microvm','idle_timeout_s':180,'max_lifetime_s':600,'quota':{'max_concurrent':1,'daily_sandbox_seconds':900,'daily_egress_bytes':8388608},'network':net},'workspace':{'max_workspaces':1,'max_bytes':1048576}}
        published=await registry.publish(Tenant(tenant_id=owner),registry_pb2.REGISTRY_KIND_TENANT_POLICY,'',policy,change_note='zero-model isolated boundary acceptance')
        report['policy_version']=published.version
        task='task-home-'+nonce;rel='.config/goal-acceptance/'+nonce+'.toml';content='value="pinned-v1"\n'
        doc={'instructions':'Fixture preference.','files':[{'path':rel,'content':content}]}
        raw=json.dumps(doc,sort_keys=True,separators=(',',':'));digest=hashlib.sha256(raw.encode()).hexdigest();now=int(time.time()*1000)
        await sql("INSERT INTO registry_entries(kind,name,version,state,task_family,document,change_note,evaluation,created_at,updated_at) VALUES (8,$1,1,2,'',$2,'zero-model fixture','',$3,$3)",'~acceptance/personal-config',raw,now)
        await sql("INSERT INTO tasks(task_id,session_id,session_key,client_request_id,status,signaled,last_event_id,created_at,submitted_by,personal_config_pin,personal_config_selection) VALUES ($1,'fixture','fixture',$1,3,1,0,$2,'acceptance',$3,'')",task,now,json.dumps({'version':'user/v1','digest':digest}))
        session='ses_01HZY2J5K3M4N5P6Q7R8S9T0VW';lease=await acquire(task,session,True)
        got=await py(lease,"import os,sys,pathlib,json;p=pathlib.Path(os.environ['HOME'])/sys.argv[1];print(json.dumps({'content':p.read_text(),'mode':p.stat().st_mode&511}))",rel)
        assert got=={'content':content,'mode':0o600},got
        assert lease.environment['personal_config']['digest']==digest
        await py(lease,"import os,sys,pathlib,json;p=pathlib.Path(os.environ['HOME'])/sys.argv[1];p.write_text('existing-wins');print(json.dumps(True))",rel)
        await broker.release(lease,keep=True)
        lease=await acquire(task,session,True)
        got=await py(lease,"import os,sys,pathlib,json;print(json.dumps((pathlib.Path(os.environ['HOME'])/sys.argv[1]).read_text()))",rel)
        assert got=='existing-wins'
        assert rel in lease.environment['personal_config']['skipped']
        await broker.release(lease,keep=False,skip_snapshot=True)
        doc['files'][0]['content']='value="new-default-v2"\n';raw2=json.dumps(doc,sort_keys=True,separators=(',',':'))
        await sql("UPDATE registry_entries SET state=3 WHERE kind=8")
        await sql("INSERT INTO registry_entries(kind,name,version,state,task_family,document,change_note,evaluation,created_at,updated_at) VALUES (8,$1,2,2,'',$2,'zero-model fixture','',$3,$3)",'~acceptance/personal-config',raw2,now+1)
        lease=await acquire(task)
        assert await py(lease,"import os,sys,pathlib,json;print(json.dumps((pathlib.Path(os.environ['HOME'])/sys.argv[1]).read_text()))",rel)==content
        assert lease.environment['personal_config']['version']=='user/v1'
        await broker.release(lease,keep=False,skip_snapshot=True)
        report['checks']['D-27-HOME']={'version':'user/v1','digest':digest,'mode':'0600','existing_file_preserved':True,'new_default_does_not_change_pin':True,'fixture_task_not_submitted':True}
        print(json.dumps({'event':'check_completed','tenant':owner,'check':'D-27-HOME'}),flush=True)
        network=NetworkPolicy.from_policy(net);request_config=check_request(network,scope='public',minutes=15,reason='isolated production acceptance',effect_ceiling='write_reversible')
        nettask='task-net-'+nonce
        grant=await grants.create(owner,request_config,network,task_id=nettask,approved_by='acceptance')
        lease=await acquire(nettask)
        public=await request(lease,'https://example.com/')
        assert public['status']=='200' and public['exit']==0,public
        blocked={}
        for target in ['http://169.254.169.254/','http://127.0.0.1/','http://10.0.0.1/','http://localhost/','http://kubernetes.default.svc/','http://example.com:25/']:
            result=await request(lease,target)
            assert result['status']=='403' and result['gateway_denied'],(target,result)
            blocked[target]=result
        await grants.revoke_task(owner,nettask)
        revoked=await wait_denied(lease,'http://example.com/')
        await broker.release(lease,keep=False,skip_snapshot=True)
        expired_task='task-exp-'+nonce
        exp=await grants.create(owner,request_config,network,task_id=expired_task,approved_by='acceptance')
        # Test only this grant's deadline using a fixture timestamp, never host time.
        await sql('UPDATE sandbox_grants SET expires_at=$1 WHERE grant_id=$2',iso(utc_now()+timedelta(seconds=2)),exp.grant_id)
        lease=await acquire(expired_task);await asyncio.sleep(3)
        expired=await wait_denied(lease,'http://example.com/')
        await broker.release(lease,keep=False,skip_snapshot=True)
        quota_task='task-quota-'+nonce;quota_policy=dataclasses.replace(network,max_down_bytes=1)
        await grants.create(owner,request_config,quota_policy,task_id=quota_task,approved_by='acceptance')
        lease=await acquire(quota_task);quota=await request(lease,'https://example.com/')
        assert quota['exit']!=0 or quota['status']!='200',quota
        await broker.release(lease,keep=False,skip_snapshot=True)
        audits=[]
        for _ in range(6):
            audits=await query('SELECT host,port,result,reason FROM sandbox_egress_audit')
            if any('bytes_exceeded' in x['reason'] for x in audits):break
            await asyncio.sleep(2)
        assert any('bytes_exceeded' in x['reason'] for x in audits),'quota audit missing'
        assert any(x['reason']=='public_ip_literal' for x in audits),'private IP rejection evidence missing'
        report['checks']['NET-OPEN']={'public':public,'private_and_metadata_denied':blocked,'revoked':revoked,'expired':expired,'byte_quota':quota,'audits':[dict(x) for x in audits],'grant_scope':'public','expiry_fixture':'own row deadline set to now+2seconds; host clock unchanged'}
        assert not await query('SELECT reservation_id FROM model_usage')
        report['checks']['zero_model_usage_rows']=True
    except BaseException as exc:
        error=exc;report['failure']={'type':type(exc).__name__,'code':getattr(exc,'code',None),'message':str(exc)[:500]}
    finally:
        try:
            await broker.destroy_tenant(owner)
            assert not await query("SELECT env_ref FROM sandbox_envs WHERE state IN ('ready','creating','starting','destroying')")
            report['cleanup']['envs_destroyed']=True
            report['sandbox_usage']=[dict(x) for x in await query('SELECT * FROM sandbox_usage')]
            for table in ['sandbox_transfers','tasks','registry_entries','sandbox_grants','sandbox_egress_audit','sandbox_envs','sandbox_usage','sandbox_quota_lock']:
                await sql('DELETE FROM '+table)
            async with db.bind_platform('boundary-acceptance-cleanup').transaction() as tx:
                await tx.execute("DELETE FROM sandbox_env_directory WHERE owner=$1 AND state IN ('destroyed','failed','lost')",(owner,))
            assert not (await bucket.list(prefix='',limit=1)).items
            report['cleanup']['scoped_rows_deleted']=True;report['cleanup']['objects_empty']=True
        except BaseException as exc:
            report['cleanup']['failure']={'type':type(exc).__name__,'message':str(exc)[:500]};error=error or exc
        await broker.aclose();await registry.aclose();await db.aclose();await broker.archiver._store.aclose()
        report['elapsed_seconds']=round(time.time()-report['started_unix'],3);report['passed']=error is None
        print(json.dumps(report,ensure_ascii=False,default=str),flush=True)
    return 0 if error is None else 1
if __name__=='__main__':raise SystemExit(asyncio.run(main()))
