"""Task-pinned file tools over the authenticated File API, never direct host I/O."""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from agentruntime_core.enums import Effect
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError, TransientError
from agentruntime_core.providers.tool_driver import ToolSpec
from agentruntime_core.tenancy import TenantContext


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None


class FileTools:
    def __init__(self, base, key, attachments, db, service_token=""):
        parsed=urllib.parse.urlsplit(base)
        if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError('invalid File API base URL')
        if parsed.query or parsed.fragment:
            raise ValueError('File API base URL must not contain query or fragment')
        self.base=base.rstrip('/');self.key=key;self.attachments=attachments;self.db=db;self.service_token=service_token

    async def scope(self,invocation):
        if not invocation.tenant_id or not invocation.task_id or not invocation.workspace_id:
            raise DeniedError('file_task_scope_required','file tools require a task and workspace scope')
        refs=await self.attachments.references(invocation.tenant_id,invocation.task_id,invocation.workspace_id)
        if not refs:raise NotFoundError('file_not_pinned','current task has no pinned file sources')
        tenant=TenantContext(tenant_id=invocation.tenant_id,workspace_id=invocation.workspace_id)
        async with self.db.bind(tenant).transaction(read_only=True) as tx:
            task=await tx.query_one('SELECT submitted_by FROM tasks WHERE task_id=$1',(invocation.task_id,))
            actor=str(task['submitted_by'] if task else '')
            if not actor or invocation.user_id and invocation.user_id!=actor:
                raise DeniedError('file_task_actor_mismatch','file tool actor differs from task submitter')
            roots={}
            for uri in refs:
                rows=await tx.query("SELECT f.file_id,f.name,v.version_id,v.mime_type FROM runtime_files f JOIN runtime_file_versions v ON f.file_id=v.file_id WHERE f.workspace_id=$1 AND f.lifecycle='active' AND (v.object_uri=$2 OR (v.parent_version_id='' AND EXISTS(SELECT 1 FROM runtime_file_sources s WHERE s.file_id=f.file_id AND s.object_uri=$2)))",(invocation.workspace_id,uri))
                if len(rows)!=1:raise NotFoundError('file_not_pinned','pinned file version cannot be resolved')
                roots[uri]={'file_id':rows[0]['file_id'],'version_id':rows[0]['version_id'],'_name':rows[0]['name'],'_mime':rows[0]['mime_type']}
        return actor,roots

    async def http(self,invocation,actor,method,path,body=None):
        # Same short-lived HMAC identity format as the existing Worker client;
        # signing material never enters tool arguments, results or logs.
        from agentruntime_worker.memory import WorkerMemoryClient
        headers={'Content-Type':'application/json'}
        if self.service_token:
            headers.update({'x-ar-file-token':self.service_token,'x-ar-file-tenant':invocation.tenant_id,'x-ar-file-user':actor})
        else:
            headers['Authorization']=dict(WorkerMemoryClient('',self.key).metadata(invocation.tenant_id,actor))['authorization']
        url=self.base+path+'?'+urllib.parse.urlencode({'workspace_id':invocation.workspace_id})
        payload=None if body is None else json.dumps(body,ensure_ascii=False).encode()
        def request():
            req=urllib.request.Request(url,data=payload,method=method,headers=headers)
            try:
                with urllib.request.build_opener(NoRedirect()).open(req,timeout=5) as response:
                    raw=response.read((16<<20)+1)
            except urllib.error.HTTPError as error:
                if error.code in (401,403):raise DeniedError('file_api_denied','file API denied this task operation') from None
                if error.code==404:raise NotFoundError('file_not_found','file operation is unavailable') from None
                if error.code in (408,429,500,502,503,504):raise TransientError('file_api_unavailable','file API is temporarily unavailable') from None
                raise InvalidError('file_api_rejected','file API rejected the operation') from None
            except (urllib.error.URLError,TimeoutError):
                raise TransientError('file_api_unavailable','file API is temporarily unavailable') from None
            if len(raw)>16<<20:raise InvalidError('file_result_too_large','file API response exceeds limit')
            value=json.loads(raw)
            if not isinstance(value,dict):raise InvalidError('invalid_file_result','invalid file API result')
            return value
        return await asyncio.to_thread(request)

    @staticmethod
    def checked_job(job,roots):
        allowed={(r['file_id'],r['version_id']) for r in roots.values()}
        inputs=job.get('inputs')
        if not isinstance(inputs,list) or not inputs or any((r.get('file_id'),r.get('version_id')) not in allowed for r in inputs):
            raise DeniedError('file_job_outside_task','file job references sources not pinned to this task')
        return job

    async def result(self,invocation,actor,job):
        if job.get('operation')=='render' and job.get('status') in ('completed','ready','partial'):
            images=[a for a in job.get('result',{}).get('artifacts',[]) if a.get('mime_type') in ('image/png','image/jpeg','image/webp')]
            if images:
                attached=await self.http(invocation,actor,'POST','/v1/file-jobs/'+job['id']+'/attach',{'task_id':invocation.task_id,'artifact_ids':[a['id'] for a in images]})
                job={**job,'attached_images':attached.get('attached',[])}
        return job

    async def invoke(self,invocation,operation):
        actor,roots=await self.scope(invocation)
        args=invocation.arguments
        from .scopes import CHILD_FILE_SCOPE, restrict
        child_scope=CHILD_FILE_SCOPE.get()
        if child_scope is not None:
            args=restrict('table_query' if operation=='query' else 'file_'+operation,args,child_scope)
            selected=roots.get(args['path'])
            if not selected or not str(selected.get('_name','')).lower().endswith('.xlsx') or str(selected.get('_mime','')).split(';',1)[0] not in ('application/zip','application/octet-stream','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'):
                raise DeniedError('child_file_type_denied','whole-sheet delegation currently accepts only XLSX sources')
        job_id=args.get('job_id')
        if job_id:
            if operation!='inspect' or not isinstance(job_id,str) or not __import__('re').fullmatch(r'fjob_[a-f0-9]{48}',job_id):
                raise InvalidError('invalid_file_job','invalid file job identity')
            job=await self.http(invocation,actor,'GET','/v1/file-jobs/'+job_id)
            return await self.result(invocation,actor,self.checked_job(job,roots))
        paths=args.get('paths') or [args.get('path')]
        if not isinstance(paths,list) or not 1<=len(paths)<=10 or any(path not in roots for path in paths):
            raise NotFoundError('file_not_pinned','choose only URIs in the current task attachment manifest')
        inputs=[{k:v for k,v in roots[path].items() if not k.startswith('_')} for path in dict.fromkeys(paths)]
        options=dict(args.get('options') or {})
        if operation=='query' and args.get('sql'):options['sql']=args['sql']
        if operation=='edit' and args.get('operations'):options['operations']=args['operations']
        body={'operation':operation,'inputs':inputs,'selector':args.get('selector') or {},'options':options}
        key=invocation.idempotency_key or hashlib.sha256(json.dumps([invocation.task_id,body],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        body['request_id']='tool-'+hashlib.sha256((invocation.task_id+':'+key).encode()).hexdigest()[:48]
        job=await self.http(invocation,actor,'POST','/v1/file-jobs',body)
        self.checked_job(job,roots)
        # Bounded wait. Long work returns a real handle for file_inspect(job_id).
        try:
            for _ in range(6):
                if job.get('status') not in ('pending','running'):return await self.result(invocation,actor,job)
                await asyncio.sleep(2)
                _,current_roots=await self.scope(invocation)
                job=await self.http(invocation,actor,'GET','/v1/file-jobs/'+urllib.parse.quote(job['id'],safe=''))
                self.checked_job(job,current_roots)
        except asyncio.CancelledError:
            # Drop only this consumer; the API decides whether shared work ends.
            try:
                await asyncio.shield(self.http(invocation,actor,'POST','/v1/file-jobs/'+job['id']+'/cancel',{'consumer_id':body['request_id']}))
            except Exception:
                pass
            raise
        return {**job,'next_action':'Repeat the same delegated file operation to check its result; pending is not completion.' if child_scope is not None else 'Use file_inspect with this job_id to check its actual result; pending is not completion.'}



    async def knowledge(self,invocation,resolve=False):
        if not invocation.tenant_id or not invocation.task_id or not invocation.workspace_id:
            raise DeniedError('file_task_scope_required','knowledge tools require a current task')
        tenant=TenantContext(tenant_id=invocation.tenant_id,workspace_id=invocation.workspace_id)
        async with self.db.bind(tenant).transaction(read_only=True) as tx:
            task=await tx.query_one('SELECT submitted_by FROM tasks WHERE task_id=$1',(invocation.task_id,))
            actor=str(task['submitted_by'] if task else '')
            if not actor or invocation.user_id and invocation.user_id!=actor:
                raise DeniedError('file_task_actor_mismatch','knowledge actor differs from task submitter')
            pinned=await tx.query_one('SELECT manifest FROM runtime_file_collection_freezes WHERE task_id=$1 AND workspace_id=$2 AND created_by=$3',(invocation.task_id,invocation.workspace_id,actor))
            if not pinned or not json.loads(pinned['manifest']):
                raise NotFoundError('collection_not_pinned','task has no explicitly pinned knowledge collection')
        body={'task_id':invocation.task_id}
        if resolve:
            ids=invocation.arguments.get('node_ids')
            if not isinstance(ids,list) or not 1<=len(ids)<=20 or any(not isinstance(value,str) or not value or len(value)>256 for value in ids):
                raise InvalidError('invalid_evidence_ids','select 1 to 20 retrieved node identities')
            body['node_ids']=ids
        else:
            query=invocation.arguments.get('query','');limit=invocation.arguments.get('limit',8)
            if not isinstance(query,str) or not query.strip() or len(query)>8000 or isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=20:
                raise InvalidError('invalid_search','query and limit are invalid')
            body.update(query=query,limit=limit)
        # No client generation, embedding version or collection filter is passed.
        return await self.http(invocation,actor,'POST','/v1/knowledge/search',body)

def register_file_tools(provider,config):
    base=os.environ.get('AR_TASK_SERVICE_URL','') or config.task_service_internal_url
    service_token=os.environ.get('AR_FILE_INTERNAL_TOKEN','')
    if not base or not (config.dev_signing_key or service_token):return []
    from .authority import task_attachments
    from agentruntime_worker.tools import _relational
    tools=FileTools(base,config.dev_signing_key.encode(),task_attachments(config),_relational(config),service_token=service_token)
    definitions=[('file_inspect','inspect',Effect.READ,'Inspect a pinned file, or check a prior file job with job_id.'),
                 ('file_read','read',Effect.READ,'Read bounded structured file content with evidence anchors; path is a pinned obj URI.'),
                 ('file_render','render',Effect.READ,'Render selected pages or crop a pinned image; returns real derived artifacts.'),
                 ('table_query','query',Effect.READ,'Compute SQL on selected pinned tables. Use paths for multi-source joins; no external file or network reads.'),
                 ('file_edit','edit',Effect.WRITE_REVERSIBLE,'Create and verify a candidate document version. Does not publish or overwrite the original.')]
    names=[]
    for name,operation,effect,description in definitions:
        properties={'path':{'type':'string'},'paths':{'type':'array','items':{'type':'string'},'minItems':1,'maxItems':10},'selector':{'type':'object'},'options':{'type':'object'}}
        if name=='file_inspect':properties['job_id']={'type':'string'}
        if name=='table_query':properties['sql']={'type':'string','maxLength':20000}
        if name=='file_edit':properties['operations']={'type':'array','items':{'type':'object'},'maxItems':100}
        async def handler(invocation,op=operation):return await tools.invoke(invocation,op)
        provider.register(ToolSpec(name=name,version='v1',description=description,input_schema={'type':'object','properties':properties,'additionalProperties':False},output_schema={'type':'object'},effect=effect,permissions=frozenset(),timeout=timedelta(seconds=90)),handler)
        names.append(name)
    for name,resolve in [('file_search',False),('evidence_resolve',True)]:
        properties={'node_ids':{'type':'array','items':{'type':'string'},'minItems':1,'maxItems':20}} if resolve else {'query':{'type':'string','minLength':1,'maxLength':8000},'limit':{'type':'integer','minimum':1,'maximum':20}}
        async def handler(invocation,res=resolve):return await tools.knowledge(invocation,res)
        provider.register(ToolSpec(name=name,version='v1',description='Resolve exact evidence nodes in the current task knowledge snapshot.' if resolve else 'Search the explicitly pinned task knowledge snapshot; returns sourced evidence, not exhaustive file coverage.',input_schema={'type':'object','required':['node_ids' if resolve else 'query'],'properties':properties,'additionalProperties':False},output_schema={'type':'object'},effect=Effect.READ,permissions=frozenset(),timeout=timedelta(seconds=30)),handler)
        names.append(name)
    return names
