"""Run inside the final worker Pod only after the deployment/resource-window gate.
No model calls. Creates its own isolated tenant, freezes model_allowlist=[],
uses production Sandbox/Registry credentials without printing them, and cleans
only rows/objects belonging to the generated tenant. Emits one JSON report.
Host orchestrator must remove report['owned_cache_blobs'] from the FC cache only
after cleanup.envs_destroyed is true, validating digest/size before unlinking.
"""
import asyncio, copy, dataclasses, hashlib, json, os, secrets, time, urllib.request
from datetime import timedelta
from agentruntime.common.v1 import tenant_pb2
from agentruntime.registry.v1 import registry_pb2
from agentruntime_core.errors import ProviderError
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.tenancy import TenantContext
from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.registry.client import GrpcRegistry
from agentruntime_worker.tools import sandbox_broker, context_objects, dev_tools, _relational, _platform_secret

async def main():
    if os.environ.get('AR_TRANSFER_ACCEPT_EXECUTE') != '1':
        raise RuntimeError('final-deployment acceptance gate has not been enabled')
    started=time.time()
    nonce=secrets.token_hex(8)
    owner='transfer-accept-'+nonce
    workspace='workspace'
    task='task-transfer-'+nonce
    cfg=dataclasses.replace(WorkerConfig.from_env(os.environ),tool_workspace_root=None,sandbox_sessions=True,sandbox_runtime='universal')
    broker=sandbox_broker(cfg)
    assert broker is not None
    objects=context_objects(cfg)
    bucket=objects.bind(TenantContext(tenant_id=owner))
    db=_relational(cfg)
    scoped=db.bind(TenantContext(tenant_id=owner))
    token=await _platform_secret(cfg,'registry/publish-token')
    if not token:raise RuntimeError('Registry publisher credential unavailable')
    registry=GrpcRegistry(cfg.registry_address,publish_token=token.decode())
    fc_token=await _platform_secret(cfg,'sandbox/fc-manager-token')
    fc_url=os.environ['AR_TRANSFER_FC_URL'].rstrip('/')
    if not fc_token:raise RuntimeError('FC manager credential unavailable')
    http=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def stats():
        req=urllib.request.Request(fc_url+'/v1/blob-cache',headers={'Authorization':'Bearer '+fc_token.decode(),'X-Sandbox-Owner':'_platform'})
        with http.open(req,timeout=15) as res:return json.load(res)
    report={'tenant':owner,'workspace':workspace,'task':task,'started_unix':started,'model_calls':0,'model_cost_usd':0,'owned_cache_blobs':{},'checks':{},'cleanup':{}}
    # Persist the exact cleanup scope in the host's log before any mutation.
    print(json.dumps({'event':'acceptance_started','tenant':owner,'workspace':workspace,'task':task,'started_unix':started}),flush=True)
    async def rows(sql,*args):
        async with scoped.transaction(read_only=True) as tx:return await tx.query(sql,args)
    session_ids=['ses_01HZY2J5K3M4N5P6Q7R8S9T0VW','ses_01HZY2J5K3M4N5P6Q7R8S9T0VX','ses_01HZY2J5K3M4N5P6Q7R8S9T0VY','ses_01HZY2J5K3M4N5P6Q7R8S9T0VZ','ses_01HZY2J5K3M4N5P6Q7R8S9T0W0']
    async def acquire(index,writable=False):
        tc=TenantContext(tenant_id=owner,user_id='acceptance',workspace_id=workspace,session_id=session_ids[index])
        spec=SandboxSpec(runtime='universal',cpu_millis=1000,memory_mb=512,network='none',idle_timeout=timedelta(minutes=5),workspace_id=workspace,session_id=session_ids[index])
        # Match production session tools: the initial writable environment must
        # retain its session ID so deliver_file reads the unarchived live file.
        lease=await broker.acquire(tc,spec,task_id=task,writable=writable,reuse=index==0,busy_for=timedelta(minutes=3))
        assert lease.backend.kind=='firecracker' and lease.archive
        return tc,lease
    async def digest_in(lease):
        out=await lease.box.exec(lease.env_id,['python3','-c',"import hashlib;print(hashlib.sha256(open('payload.bin','rb').read()).hexdigest())"],timeout=timedelta(seconds=30))
        assert out.exit_code==0
        return out.stdout.decode().strip()
    failure=None
    tools=None
    try:
        policy={'model_allowlist':[],'model_budget':{'daily_usd':0,'monthly_usd':0},'sandbox':{'backend':os.environ.get('AR_TRANSFER_BACKEND','firecracker'),'fallback':[],'min_isolation':'microvm','idle_timeout_s':300,'max_lifetime_s':1800,'quota':{'max_concurrent':2,'daily_sandbox_seconds':1200,'daily_egress_bytes':33554432},'network':{'allowed':False}},'workspace':{'max_workspaces':2,'max_bytes':33554432}}
        published=await registry.publish(Tenant(tenant_id=owner),registry_pb2.REGISTRY_KIND_TENANT_POLICY,'',policy,change_note='isolated zero-model transfer acceptance')
        report['policy_version']=published.version
        report['checks']['model_spending_disabled']=True
        before=stats();report['cache_before']=before
        tc,lease=await acquire(0,True)
        seed_a=hashlib.sha256((nonce+'a').encode()).digest()
        seed_b=hashlib.sha256((nonce+'b').encode()).digest()
        a=seed_a*(4194304//len(seed_a));b=seed_b*(4194304//len(seed_b))
        payload_sha=hashlib.sha256(a+b+a).hexdigest()
        out_bytes=nonce.encode()*65536;err_bytes=nonce.encode()*8192
        script="import hashlib,sys;a=bytes.fromhex(sys.argv[1])*131072;b=bytes.fromhex(sys.argv[2])*131072;open('payload.bin','wb').write(a+b+a);sys.stdout.write(sys.argv[3]*65536);sys.stderr.write(sys.argv[3]*8192)"
        result=await lease.box.exec_with_output_refs(lease.env_id,['python3','-c',script,seed_a.hex(),seed_b.hex(),nonce],timeout=timedelta(seconds=60))
        assert result.exit_code==0 and len(result.stdout)<9000 and len(result.stderr)<9000
        assert result.stdout_object and result.stderr_object and not result.stdout_pending and not result.stderr_pending
        for ref,data in [(result.stdout_object,out_bytes),(result.stderr_object,err_bytes)]:
            assert ref['size_bytes']==len(data) and ref['sha256']==hashlib.sha256(data).hexdigest()
            report['owned_cache_blobs'][ref['sha256']]=ref['size_bytes']
        report['checks']['WK-LITE-01']={'stdout_bytes':len(out_bytes),'stderr_bytes':len(err_bytes),'worker_received_bytes':len(result.stdout)+len(result.stderr)}
        await broker.release(lease,result=result,keep=True)
        tools=dev_tools(cfg)
        read=(await tools.bind(tc).invoke('context_read','v1',{'path':result.stdout_object['object_uri'],'offset':123,'max_chars':128},task_id=task)).output
        assert read['content']==out_bytes.decode()[123:251]
        artifact=(await tools.bind(tc).invoke('deliver_file','v1',{'path':'payload.bin'},idempotency_key='delivery-'+nonce,task_id=task)).output
        replay=(await tools.bind(tc).invoke('deliver_file','v1',{'path':'payload.bin'},idempotency_key='delivery-'+nonce,task_id=task)).output
        assert artifact==replay and artifact['sha256']==payload_sha and artifact['size_bytes']==len(a+b+a)
        artifacts=await rows('SELECT object_uri,sha256,size_bytes FROM task_artifacts WHERE task_id=$1',task)
        assert len(artifacts)==1
        download=await bucket.get(bucket.key_of(artifacts[0]['object_uri']))
        assert hashlib.sha256(download.data).hexdigest()==payload_sha
        report['owned_cache_blobs'][payload_sha]=len(download.data)
        report['checks']['WK-LITE-02']={'artifacts':1,'sha256':payload_sha,'bytes':len(download.data),'idempotent':True}
        one=await broker.snapshot(lease);two=await broker.snapshot(lease)
        snapshots=await rows('SELECT snapshot_id,manifest_key,new_blobs FROM workspace_snapshots WHERE workspace_id=$1 ORDER BY seq',workspace)
        assert snapshots[-1]['snapshot_id']==two.snapshot_id and int(snapshots[-1]['new_blobs'])==0
        key=snapshots[-1]['manifest_key']
        manifest_bytes=(await bucket.get(key)).data
        manifest=json.loads(manifest_bytes)
        assert manifest['version']==2
        file=next(e for e in manifest['entries'] if e['path']=='payload.bin')
        assert len(file['chunks'])==3 and file['sha256']==payload_sha
        for block in file['chunks']:report['owned_cache_blobs'][block['sha256']]=block['size_bytes']
        await broker.release(lease,keep=False,skip_snapshot=True)
        restore_stats=[]
        for index in (1,2):
            stats_before=stats()
            _,restored=await acquire(index)
            assert restored.restored==two.snapshot_id and await digest_in(restored)==payload_sha
            stats_after=stats()
            # FC PUT fills the cache only after durable ObjectStore success.
            # Both fresh VMs must reuse those upload-verified local blocks.
            assert stats_after['hits']-stats_before['hits']>=2
            assert stats_after['misses']==stats_before['misses'],'restore transferred already cached blocks'
            restore_stats.append({'before':stats_before,'after':stats_after})
            await broker.release(restored,keep=False,skip_snapshot=True)
        report['checks']['WS-PULL-01']={'ordered_chunks':3,'unique_chunks':2,'second_snapshot_new_blobs':0,'two_restores_verified':True}
        report['checks']['WS-BLOB-01']={'restores':restore_stats,'cache_source':'verified PUT write-through','object_store_misses_during_restores':0}
        corrupted=copy.deepcopy(manifest);next(e for e in corrupted['entries'] if e['path']=='payload.bin')['chunks'][0]['sha256']='f'*64
        await bucket.put(key,json.dumps(corrupted).encode(),content_type='application/json')
        try:
            try:
                await acquire(3)
                raise AssertionError('corrupted snapshot unexpectedly produced a lease')
            except ProviderError as error:
                assert error.code=='workspace_restore_mismatch',error.code
            live=await rows("SELECT env_ref FROM sandbox_envs WHERE state IN ('ready','creating','starting')")
            assert not live
            report['checks']['restore_failure_no_partial_environment']=True
        finally:
            await bucket.put(key,manifest_bytes,content_type='application/json')
        _,restored=await acquire(4)
        assert await digest_in(restored)==payload_sha
        await broker.release(restored,keep=False,skip_snapshot=True)
        report['checks']['restore_recovered_after_repair']=True
        model_rows=await rows('SELECT reservation_id,actual_usd_micros FROM model_usage')
        assert not model_rows,'unexpected model calls in a zero-model acceptance tenant'
        report['checks']['zero_model_usage_rows']=True
    except BaseException as error:
        failure=error
        report['failure']={'type':type(error).__name__,'code':getattr(error,'code',None),'message':str(error)[:800]}
    finally:
        try:
            await broker.destroy_tenant(owner)
            live=await rows("SELECT env_ref FROM sandbox_envs WHERE state IN ('ready','creating','starting','destroying')")
            assert not live
            report['cleanup']['envs_destroyed']=True
            report['sandbox_usage']=await rows('SELECT * FROM sandbox_usage')
            count=0
            while True:
                page=await bucket.list(prefix='',limit=1000)
                for item in page.items:await bucket.delete(item.key);count+=1
                if not page.items:break
            assert not (await bucket.list(prefix='',limit=1)).items
            report['cleanup']['objects_deleted']=count
            tables=['sandbox_transfers','task_artifacts','task_artifact_reservations','task_artifact_quota_lock','workspace_snapshots','workspace_pushes','workspaces','sandbox_grants','sandbox_egress_audit','sandbox_envs','sandbox_usage','sandbox_quota_lock','registry_entries']
            for table in tables:
                async with scoped.transaction() as tx:await tx.execute('DELETE FROM '+table)
            platform=db.bind_platform('transfer-acceptance-cleanup')
            async with platform.transaction() as tx:
                await tx.execute("DELETE FROM sandbox_env_directory WHERE owner=$1 AND state IN ('destroyed','failed','lost')",(owner,))
            report['cleanup']['scoped_rows_deleted']=True
        except BaseException as error:
            report['cleanup']['failure']={'type':type(error).__name__,'message':str(error)[:800]}
            failure=failure or error
        await broker.aclose();await registry.aclose()
        stores={id(db):db,id(broker.archiver._store):broker.archiver._store}
        if tools is not None:
            for group in tools._tools.values():
                for tool in group.values():
                    candidate=getattr(tool.handler,'store',None)
                    if candidate is not None and hasattr(candidate,'aclose'):stores[id(candidate)]=candidate
        for store in stores.values():await store.aclose()
        report['elapsed_seconds']=round(time.time()-started,3)
        report['passed']=failure is None
        print(json.dumps(report,ensure_ascii=False,default=str),flush=True)
    return 1 if failure is not None else 0

if __name__=='__main__':raise SystemExit(asyncio.run(main()))
