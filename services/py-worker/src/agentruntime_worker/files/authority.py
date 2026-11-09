"""Current authorization for pinned Task inputs; a manifest is not a perpetual ACL."""
from __future__ import annotations
from pathlib import Path
import json
from agentruntime_core.errors import NotFoundError
from agentruntime_core.tenancy import TenantContext
from agentruntime_worker.sandbox.store import relational_store

class FileAuthority:
    def __init__(self, provider):self.provider=provider
    async def check(self,tenant_id,workspace_id,task_id,manifest,refs):
        tenant=TenantContext(tenant_id=tenant_id,workspace_id=workspace_id)
        async with self.provider.bind(tenant).transaction(read_only=True) as tx:
            task=await tx.query_one("SELECT submitted_by FROM tasks WHERE task_id=$1",(task_id,))
            actor=task.get('submitted_by') if task else ''
            if manifest.get('user_id') and manifest['user_id']!=actor:raise NotFoundError('file_actor_mismatch','任务文件身份不一致')
            if not actor:raise NotFoundError('file_access_denied','任务的文件访问身份不可用')
            pinned={v['object_uri']:v for v in manifest.get('versions',[]) if isinstance(v,dict)}
            for uri in refs:
                rows=await tx.query("SELECT f.file_id,f.created_by,f.lifecycle,f.acl_epoch,v.version_id,v.sha256 FROM runtime_files f JOIN runtime_file_versions v ON f.file_id=v.file_id WHERE f.workspace_id=$1 AND (v.object_uri=$2 OR (v.parent_version_id='' AND EXISTS(SELECT 1 FROM runtime_file_sources s WHERE s.file_id=f.file_id AND s.object_uri=$2)))",(workspace_id,uri))
                if len(rows)!=1 or rows[0]['lifecycle']!='active':raise NotFoundError('file_access_revoked','文件已撤权或删除')
                row=rows[0]
                if uri in pinned and (row['version_id']!=pinned[uri]['version_id'] or row['sha256']!=pinned[uri]['sha256']):raise NotFoundError('file_version_mismatch','文件固定版本不一致')
                if row['created_by']!=actor:
                    grant=await tx.query_one("SELECT permission FROM runtime_file_grants WHERE file_id=$1 AND workspace_id=$2 AND subject_type='user' AND subject_id=$3",(row['file_id'],workspace_id,actor))
                    if not grant:raise NotFoundError('file_access_revoked','文件访问已撤销')


    async def describe_derived(self,tenant_id,workspace_id,task_id):
        tenant=TenantContext(tenant_id=tenant_id,workspace_id=workspace_id)
        async with self.provider.bind(tenant).transaction(read_only=True) as tx:
            task=await tx.query_one("SELECT submitted_by FROM tasks WHERE task_id=$1",(task_id,))
            if not task or not task.get('submitted_by'):return []
            actor=task['submitted_by']
            rows=await tx.query("SELECT job_id,artifact_id,object_uri,roots,sha256,mime_type,size_bytes,name FROM runtime_file_task_artifacts WHERE task_id=$1 ORDER BY job_id,artifact_id",(task_id,))
            files=[]
            for item in rows:
                roots=json.loads(item['roots'])
                if not roots:raise NotFoundError('derived_file_invalid','衍生图片缺少根文件')
                for root in roots:
                    row=await tx.query_one("SELECT f.created_by,f.lifecycle,f.acl_epoch,v.sha256 FROM runtime_files f JOIN runtime_file_versions v ON f.file_id=v.file_id WHERE f.file_id=$1 AND v.version_id=$2 AND f.workspace_id=$3",(root['file_id'],root['version_id'],workspace_id))
                    if not row or row['lifecycle']!='active' or int(row['acl_epoch'])!=int(root['acl_epoch']) or row['sha256']!=root['sha256']:
                        raise NotFoundError('file_access_revoked','衍生图片根文件已撤权或改变')
                    if row['created_by']!=actor:
                        grant=await tx.query_one("SELECT permission FROM runtime_file_grants WHERE file_id=$1 AND workspace_id=$2 AND subject_type='user' AND subject_id=$3",(root['file_id'],workspace_id,actor))
                        if not grant:raise NotFoundError('file_access_revoked','衍生图片访问已撤销')
                files.append({'uri':item['object_uri'],'name':item['name'],'media_type':item['mime_type'],'size_bytes':int(item['size_bytes']),'sha256':item['sha256'],'derived':True})
            return files


    async def check_collections(self, tenant_id, workspace_id, task_id, summaries):
        tenant=TenantContext(tenant_id=tenant_id,workspace_id=workspace_id)
        async with self.provider.bind(tenant).transaction(read_only=True) as tx:
            task=await tx.query_one("SELECT submitted_by FROM tasks WHERE task_id=$1",(task_id,))
            actor=task.get('submitted_by') if task else ''
            frozen=await tx.query_one("SELECT created_by,workspace_id,manifest FROM runtime_file_collection_freezes WHERE task_id=$1",(task_id,))
            if not actor or not frozen or frozen['created_by']!=actor or frozen['workspace_id']!=workspace_id:
                raise NotFoundError('collection_not_pinned','任务知识集合快照不可用')
            pinned={item['id']:item for item in json.loads(frozen['manifest'])}
            for summary in summaries:
                item=pinned.get(summary.get('id'))
                if not item or any(item.get(key)!=summary.get(key) for key in ('generation','embedding_version','revision')):
                    raise NotFoundError('collection_snapshot_mismatch','知识集合快照不一致')
                for root in item.get('roots',[]):
                    row=await tx.query_one("SELECT f.created_by,f.lifecycle,f.acl_epoch,v.sha256 FROM runtime_files f JOIN runtime_file_versions v ON f.file_id=v.file_id WHERE f.file_id=$1 AND v.version_id=$2 AND f.workspace_id=$3",(root['file_id'],root['version_id'],workspace_id))
                    if not row or row['lifecycle']!='active' or int(row['acl_epoch'])!=int(root['acl_epoch']) or row['sha256']!=root['sha256']:
                        raise NotFoundError('collection_access_revoked','知识集合来源已撤权或改变')
                    if row['created_by']!=actor:
                        grant=await tx.query_one("SELECT permission FROM runtime_file_grants WHERE file_id=$1 AND workspace_id=$2 AND subject_type='user' AND subject_id=$3",(root['file_id'],workspace_id,actor))
                        if not grant:raise NotFoundError('collection_access_revoked','知识集合来源访问已撤销')

def task_attachments(config,objects=None):
    from agentruntime_worker.tools import context_objects
    from agentruntime_worker.tools.attachments import TaskAttachments
    from agentruntime_worker.executor.dev import data_root
    return TaskAttachments(objects or context_objects(config), FileAuthority(relational_store(config.relational_dsn,data_root(config))))
