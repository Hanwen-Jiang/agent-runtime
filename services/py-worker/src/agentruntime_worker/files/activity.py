"""Trusted bridge: current ACL -> immutable byte subset -> isolated engine -> immutable outputs.

The production path exclusively uses Sandbox Service, no mounted user workspace,
no network, no environment preparation or injected project setup. The explicit
local Python setting is development-only and is never selected implicitly.
"""
from __future__ import annotations
import asyncio
import contextlib
import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Struct
from temporalio import activity
from agentruntime_core.errors import DeniedError, InvalidError, ProviderError, TransientError
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.tenancy import TenantContext
from agentruntime_worker.sandbox.store import relational_store

OPERATIONS={"query":"table.query","edit":"document.edit_apply","recalculate":"table.recalculate","notebook_execute":"notebook.execute","index":"read"}

class FileActivities:
    def __init__(self, config, objects, broker):
        self.config,self.objects,self.broker=config,objects,broker
        self.db=relational_store(config.relational_dsn,config.data_dir or Path(tempfile.mkdtemp(prefix="ar-files-")))

    async def authorize(self, request):
        t=TenantContext(tenant_id=request['tenant_id'],workspace_id=request['workspace_id'],user_id=request['user_id'])
        async with self.db.bind(t).transaction(read_only=True) as tx:
            row=await tx.query_one("SELECT requested_by,workspace_id,inputs,status,operation,request FROM runtime_file_jobs WHERE job_id=$1",(request['job_id'],))
            if not row or row['requested_by']!=t.user_id or row['workspace_id']!=t.workspace_id or row['status'] not in ('pending','running'):
                raise DeniedError('file_job_unavailable','file operation is no longer active')
            roots=json.loads(row['inputs'])
            if roots!=request['inputs'] or row['operation']!=request['operation'] or json.loads(row['request'])!=request.get('request',{}):
                raise DeniedError('file_inputs_changed','file input snapshot mismatch')
            if not roots or len(roots)>10:
                raise InvalidError('invalid_file_roots','file job requires 1 to 10 pinned roots')
            for root_index,root in enumerate(roots):
                f=await tx.query_one("SELECT f.created_by,f.acl_epoch,f.lifecycle,v.sha256,v.object_uri FROM runtime_files f JOIN runtime_file_versions v ON f.file_id=v.file_id WHERE f.file_id=$1 AND v.version_id=$2 AND f.workspace_id=$3",(root['file_id'],root['version_id'],t.workspace_id))
                if not f or f['lifecycle']!='active' or int(f['acl_epoch'])!=int(root['acl_epoch']) or f['sha256']!=root['sha256'] or f['object_uri']!=root['object_uri']:
                    raise DeniedError('file_snapshot_stale','file permission or version is no longer available')
                if f['created_by']!=t.user_id:
                    g=await tx.query_one("SELECT permission FROM runtime_file_grants WHERE file_id=$1 AND workspace_id=$2 AND subject_type='user' AND subject_id=$3",(root['file_id'],t.workspace_id,t.user_id))
                    if not g or (root_index==0 and request['operation'] in ('edit','recalculate','notebook_execute') and g['permission']!='write'):
                        raise DeniedError('file_access_revoked','file access revoked')
        return t

    @activity.defn(name='file_operation')
    async def run(self, message: Struct) -> Struct:
        request=json_format.MessageToDict(message)
        async def heartbeat():
            while True:
                activity.heartbeat({'job_id':request.get('job_id',''),'phase':'processing'})
                await asyncio.sleep(10)
        if not os.environ.get('AR_TASK_SERVICE_URL') or not os.environ.get('AR_FILE_INTERNAL_TOKEN'):
            raise InvalidError('file_completion_unconfigured', 'file activity requires the internal File API configuration')
        pulse=asyncio.create_task(heartbeat()) if activity.in_activity() else None
        try:
            try:
                result=await self.process(request)
            except TransientError:
                raise
            except ProviderError as error:
                result={'status':'failed','error':{'code':error.code,'message':error.message}}
            result = await self.complete(request, result)
            return json_format.ParseDict(result,Struct())
        finally:
            if pulse is not None:
                pulse.cancel()
                with contextlib.suppress(asyncio.CancelledError):await pulse

    async def complete(self, request, result):
        """Persist terminal state even when no UI is polling this workflow."""
        base = os.environ.get('AR_TASK_SERVICE_URL', '').rstrip('/')
        token = os.environ.get('AR_FILE_INTERNAL_TOKEN', '')
        if not base or not token:
            raise InvalidError('file_completion_unconfigured', 'file completion requires the internal File API configuration')
        url = base + '/v1/file-jobs/' + urllib.parse.quote(request['job_id'], safe='') + '/complete?' + urllib.parse.urlencode({'workspace_id': request['workspace_id']})
        payload = json.dumps({'result': result}, ensure_ascii=False, allow_nan=False).encode()
        if len(payload) > 16 << 20:
            raise InvalidError('file_result_too_large', 'file completion exceeds 16 MiB')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl): return None
        def send():
            message = urllib.request.Request(url, data=payload, method='POST', headers={'Content-Type':'application/json',
                'X-Ar-File-Token':token,'X-Ar-File-Tenant':request['tenant_id'],'X-Ar-File-User':request['user_id']})
            try:
                with urllib.request.build_opener(NoRedirect()).open(message, timeout=30) as response:
                    response.read(4096)
                return 'completed'
            except urllib.error.HTTPError as error:
                if error.code in (403,404,409): return 'revoked'
                if error.code in (408,429,500,502,503,504): return 'retry'
                raise InvalidError('file_completion_rejected', 'File API rejected the completed result') from None
            except (urllib.error.URLError, TimeoutError): return 'retry'
        for attempt in range(3):
            state = await asyncio.to_thread(send)
            if state == 'completed': return result
            if state == 'revoked':
                return {'status':'failed','error':{'code':'file_completion_revoked','message':'file operation was cancelled or its access was revoked'}}
            if attempt < 2: await asyncio.sleep(attempt + 1)
        # Preserve the completed bytes in Temporal's result: GetJob can reconcile
        # them without rerunning the parser. Do not claim the DB was updated.
        return {**result,'completion_pending':True}

    async def process(self, request: dict[str,Any]):
        tenant=await self.authorize(request)
        store=self.objects.bind(tenant)
        blobs=[]
        for root in request['inputs']:
            obj=await store.get(store.key_of(root['object_uri']))
            if len(obj.data)!=int(root['size_bytes']) or hashlib.sha256(obj.data).hexdigest()!=root['sha256'] or len(obj.data)>50<<20:
                raise InvalidError('file_integrity_mismatch','source bytes do not match immutable version')
            ext=PurePosixPath(root['name']).suffix.lower()
            if not re.fullmatch(r'\.[a-z0-9]{1,12}',ext):ext='.bin'
            blobs.append((f"input/{root['version_id']}{ext}",obj.data))
        options=dict(request.get('request',{}).get('options') or {})
        # The browser supplies selectors and calculation options, never paths.
        for key in ('path','input_root','output_dir','datasets','tables','image_path','candidate_path','base_path','execute_authorized'):
            options.pop(key,None)
        if request['operation']=='query':
            options['tables']=[{'alias':f'data_{i+2}','path':path} for i,(path,_) in enumerate(blobs[1:])]
        if request['operation']=='edit':
            allowed_images={root['version_id']:path for root,(path,_) in zip(request['inputs'][1:],blobs[1:])}
            operations=[]
            for operation in options.get('operations',[]):
                operation=dict(operation)
                operation.pop('image_path',None)
                if 'image_version_id' in operation:
                    image_id=operation.pop('image_version_id')
                    if image_id not in allowed_images:raise DeniedError('image_not_pinned','replacement image is not a selected job input')
                    operation['image_path']=allowed_images[image_id]
                operations.append(operation)
            options['operations']=operations
            options['base_sha256']=request['inputs'][0]['sha256']
        op=OPERATIONS.get(request['operation'],request['operation'])
        engine={'operation':op,'path':blobs[0][0],'input_root':'input','output_dir':'output',
                'selector':request.get('request',{}).get('selector') or {},'options':options}
        lease=None
        local=os.environ.get('AR_FILE_ENGINES_LOCAL_PYTHON','')
        if local and os.environ.get('AR_PROFILE')=='production':
            raise DeniedError('local_file_engine_forbidden','production file processing requires Sandbox Service')
        if request['operation']=='notebook_execute' and self.broker is None:
            raise DeniedError('notebook_requires_sandbox','Notebook execution requires an isolated Sandbox Service backend')
        temp=None
        try:
            if self.broker:
                spec=SandboxSpec(runtime=os.environ.get('AR_FILE_ENGINES_RUNTIME','file-engines'),cpu_millis=2000,memory_mb=4096,network='none',idle_timeout=timedelta(minutes=18))
                sandbox_tenant=TenantContext(tenant_id=tenant.tenant_id,user_id=tenant.user_id)
                lease=await self.broker.acquire(sandbox_tenant,spec,task_id=request['job_id'],writable=False,reuse=False,prepare=False,project_environment=False,busy_for=timedelta(minutes=18))
                box=lease.box
                if request['operation']=='notebook_execute':
                    if lease.network!='none' or lease.backend.kind=='local':
                        raise DeniedError('notebook_requires_sandbox','Notebook execution requires an isolated no-network backend')
                    engine['options']['execute_authorized']=True
                where=await box.exec(lease.env_id,['python3','-c','import os;print(os.getcwd())'],timeout=timedelta(seconds=15))
                if where.exit_code!=0:raise InvalidError('file_engine_unavailable','file engine interpreter unavailable')
                cwd=PurePosixPath(where.stdout.decode().strip())
                if not cwd.is_absolute():raise InvalidError('file_engine_unavailable','invalid engine root')
                for path,data in blobs:await box.write_file(lease.env_id,path,data)
                # Permissions plus digest revalidation prevent parsers changing originals.
                prep=await box.exec(lease.env_id,['python3','-c','import pathlib;pathlib.Path("output").mkdir(exist_ok=True);[p.chmod(0o400) for p in pathlib.Path("input").iterdir()]'],timeout=timedelta(seconds=15))
                if prep.exit_code!=0:raise InvalidError('file_engine_unavailable','file engine preparation failed')
                async def execute(payload):
                    response=await box.exec(lease.env_id,['python3','-m','agentruntime_worker.files.cli'],stdin=json.dumps(payload).encode(),timeout=timedelta(minutes=14))
                    if response.timed_out or response.exit_code!=0:raise InvalidError('file_engine_failed','file engine failed or exceeded time limit')
                    return response.stdout
                raw=await execute(engine)
                async def read(path):return await box.read_file(lease.env_id,path)
                async def write(path,data):return await box.write_file(lease.env_id,path,data)
            elif local:
                temp=tempfile.TemporaryDirectory(prefix='ar-file-operation-');cwd=PurePosixPath(Path(temp.name).resolve())
                for path,data in blobs:
                    p=Path(temp.name)/path;p.parent.mkdir(exist_ok=True);p.write_bytes(data);p.chmod(0o400)
                (Path(temp.name)/'output').mkdir()
                safe_env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','PYTHONPATH','AR_FILE_MODELS_DIR','AR_FILE_DOCLING_MODELS','AR_FILE_RAPIDOCR_MODELS','AR_FILE_LIBREOFFICE_BIN','AR_FILE_UNO_PYTHON','AR_FILE_POPPLER_BIN','FILE_ENGINES_MODEL_DIR','AR_UNO_PYTHON','AR_SOFFICE_BIN','HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE','TMPDIR')}
                safe_env.update({'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','HOME':temp.name})
                async def execute(payload):
                    proc=await asyncio.create_subprocess_exec(local,'-m','agentruntime_worker.files.cli',cwd=temp.name,env=safe_env,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
                    try:raw,_=await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()),14*60)
                    except BaseException:
                        proc.kill();await proc.wait();raise
                    if proc.returncode:raise InvalidError('file_engine_failed','local file engine failed')
                    return raw
                raw=await execute(engine)
                async def write(path,data):
                    file=Path(temp.name)/path;file.write_bytes(data);file.chmod(0o400)
                async def read(path):
                    file=(Path(temp.name)/path).resolve(strict=True)
                    if not file.is_relative_to(Path(temp.name).resolve()):raise InvalidError('invalid_output','artifact resolves outside operation sandbox')
                    return file.read_bytes()
            else:
                raise InvalidError('file_engine_unconfigured','file-engines sandbox runtime is not configured')
            if len(raw)>16<<20:raise InvalidError('file_result_too_large','file engine response exceeds 16 MiB')
            result=json.loads(raw)
            if not isinstance(result,dict) or result.get('status') not in ('ready','partial','failed','unsupported','cancelled','verified'):
                raise InvalidError('invalid_file_result','file engine returned an invalid result')
            if request['operation']=='edit' and options.get('fidelity')=='L2' and isinstance(result.get('candidate'),dict):
                await self.authorize(request)
                candidate=result['candidate'];path=PurePosixPath(str(candidate.get('path','')))
                if not path.is_absolute():path=cwd/path
                try:relative=path.relative_to(cwd/'output')
                except ValueError:raise InvalidError('invalid_candidate','candidate escaped output directory') from None
                if '..' in relative.parts:raise InvalidError('invalid_candidate','candidate escaped output directory')
                candidate_bytes=await read(str(PurePosixPath('output')/relative))
                if len(candidate_bytes)>50<<20 or hashlib.sha256(candidate_bytes).hexdigest()!=candidate.get('sha256'):
                    raise InvalidError('invalid_candidate','candidate integrity check failed')
                candidate_input='input/candidate'+PurePosixPath(blobs[0][0]).suffix
                await write(candidate_input,candidate_bytes)
                verify={'operation':'document.edit_verify','path':blobs[0][0],'input_root':'input','output_dir':'output/verify','selector':engine['selector'],
                        'options':{**options,'candidate_path':candidate_input,'base_sha256':request['inputs'][0]['sha256'],'candidate_sha256':candidate['sha256'],'fidelity':'L2'}}
                verified_raw=await execute(verify)
                if len(verified_raw)>16<<20:raise InvalidError('file_result_too_large','verification response too large')
                verified=json.loads(verified_raw)
                verification=verified.get('verification') or {'passed':False,'status':'failed','error':verified.get('error')}
                if verification.get('passed') and (verification.get('base_sha256')!=request['inputs'][0]['sha256'] or verification.get('candidate_sha256')!=candidate['sha256']):
                    raise InvalidError('invalid_verification','verification is not bound to the fixed source and candidate')
                result['verification']=verification
                result['status']='ready' if verification.get('passed') else 'partial'
                result['artifacts'].extend(verified.get('artifacts',[]))
                if verification.get('passed'):result['warnings']=[w for w in result.get('warnings',[]) if w.get('code')!='visual_verification_required']
            for path,data in blobs:
                if hashlib.sha256(await read(path)).digest()!=hashlib.sha256(data).digest():raise InvalidError('source_modified','file engine modified immutable source')
            await self.authorize(request)
            total=0;count=0;uploaded={}
            async def publish(entry):
                nonlocal total,count
                path=PurePosixPath(str(entry.get('path','')))
                if not path.is_absolute():path=cwd/path
                try:relative=path.relative_to(cwd/'output')
                except ValueError:raise InvalidError('invalid_output','derived artifact escaped output directory') from None
                if '..' in relative.parts:raise InvalidError('invalid_output','invalid output path')
                if str(path) in uploaded:return dict(uploaded[str(path)])
                data=await read(str(PurePosixPath('output')/relative));sha=hashlib.sha256(data).hexdigest()
                if sha!=entry.get('sha256') or len(data)!=int(entry.get('size_bytes',-1)):raise InvalidError('output_integrity_mismatch','derived artifact digest mismatch')
                total+=len(data);count+=1
                if len(data)>50<<20 or total>512<<20 or count>2000:raise InvalidError('output_quota','derived artifacts exceed processing quota')
                aid='art_'+hashlib.sha256((request['job_id']+'\x00'+str(relative)+'\x00'+sha).encode()).hexdigest()[:32]
                info=await store.put(f"file-derived/{request['job_id']}/{aid}",data,content_type=entry.get('mime_type') or 'application/octet-stream',metadata={'job_id':request['job_id'],'workspace_id':request['workspace_id'],'sha256':sha})
                public={k:v for k,v in entry.items() if k not in ('path','object_uri')};public.update({'id':aid,'name':path.name,'object_uri':info.uri,'sha256':sha,'size_bytes':len(data)})
                uploaded[str(path)]=public
                return public
            result['artifacts']=[await publish(entry) for entry in result.get('artifacts',[])]
            if isinstance(result.get('candidate'),dict):result['candidate']=await publish(result['candidate'])
            # No host/sandbox paths are part of a public result.
            def scrub(value):
                if isinstance(value,dict):return {k:scrub(v) for k,v in value.items() if k not in ('path','input_root','output_dir','candidate_path','source_path')}
                if isinstance(value,list):return [scrub(v) for v in value]
                return value
            await self.authorize(request)
            return scrub(result)
        finally:
            if lease is not None:await self.broker.release(lease,keep=False,skip_snapshot=True)
            if temp is not None:temp.cleanup()

    def activities(self):return {'file_operation':self.run}
