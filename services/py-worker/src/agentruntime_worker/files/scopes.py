"""Trusted whole-sheet delegation. Never derive authority from model arguments."""
from __future__ import annotations
from contextvars import ContextVar
from copy import deepcopy
import re
import json
from agentruntime_core.errors import DeniedError

CHILD_FILE_SCOPE: ContextVar[tuple[dict, ...] | None] = ContextVar('child_file_scope', default=None)
ALLOWED_CHILD_TOOLS = frozenset({'file_read','table_query'})


def normalize(scopes) -> tuple[dict, ...]:
    if not isinstance(scopes,list) or not 1<=len(scopes)<=10:
        raise DeniedError('child_file_scope_required','isolated file operations require explicit whole-sheet delegation')
    result=[]
    for item in scopes:
        if not isinstance(item,dict) or set(item)!={'path','selector'} or not isinstance(item['path'],str) or not item['path'].startswith('obj://'):
            raise DeniedError('invalid_child_file_scope','invalid delegated file identity')
        selector=item['selector']
        if not isinstance(selector,dict) or set(selector)!={'sheet'} or not isinstance(selector['sheet'],str) or not selector['sheet'] or len(selector['sheet'])>31:
            raise DeniedError('invalid_child_file_scope','delegation supports one explicit worksheet name per scope')
        result.append({'path':item['path'],'selector':{'sheet':selector['sheet']}})
    return tuple(result)


def restrict(tool,args,scopes):
    if tool not in ALLOWED_CHILD_TOOLS:
        raise DeniedError('child_tool_denied','isolated file delegates may only use file_read and table_query')
    scopes=normalize(scopes) if isinstance(scopes,list) else scopes
    if not scopes:raise DeniedError('child_file_scope_required','missing delegated worksheet')
    args=deepcopy(dict(args))
    if args.get('job_id') or args.get('paths') or not isinstance(args.get('path'),str):
        raise DeniedError('child_file_scope_denied','child tools require one explicitly delegated source URI')
    selector=args.get('selector') or {}
    if not isinstance(selector,dict) or set(selector)-{'sheet','range'} or not isinstance(selector.get('sheet'),str):
        raise DeniedError('child_file_scope_denied','select the explicitly delegated worksheet by name; sheet_id aliases are not accepted')
    if not any(scope['path']==args['path'] and scope['selector']['sheet']==selector['sheet'] for scope in scopes):
        raise DeniedError('child_file_scope_denied','worksheet or source is outside child delegation')
    if 'range' in selector and (not isinstance(selector['range'],str) or not re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]{0,6}:[A-Z]{1,3}[1-9][0-9]{0,6}',selector['range'])):
        raise DeniedError('child_file_scope_denied','range must be a local A1 rectangle without workbook/sheet references')
    options=args.get('options') or {}
    allowed={'header','parameters','timeout_seconds','max_rows','max_result_rows','sql'} if tool=='table_query' else set()
    if not isinstance(options,dict) or set(options)-allowed:
        raise DeniedError('child_file_scope_denied','child options cannot add inputs, paths or a different selector')
    options['_restrict_sheet']=True
    args['options']=options;args['selector']=selector
    return args


async def parent_has_file_inputs(objects,tenant,task_id):
    """Read only server-pinned state; absence is different from a failed read."""
    from agentruntime_worker.tools.attachments import TaskAttachments
    from agentruntime_core.tenancy import TenantContext
    from agentruntime_core.errors import NotFoundError
    if await TaskAttachments(objects).references(tenant.tenant_id,task_id,tenant.workspace_id):
        return True
    store=objects.bind(TenantContext(tenant_id=tenant.tenant_id,workspace_id=tenant.workspace_id))
    try:
        value=await store.get(f'task-inputs/{task_id}/manifest.json')
    except NotFoundError:
        return False
    if len(value.data)>64<<10:
        raise DeniedError('invalid_child_parent_manifest','parent input manifest exceeds limit')
    manifest=json.loads(value.data)
    if manifest.get('workspace_id',tenant.workspace_id)!=tenant.workspace_id:
        raise DeniedError('invalid_child_parent_manifest','parent workspace does not match')
    return bool(manifest.get('collections'))
