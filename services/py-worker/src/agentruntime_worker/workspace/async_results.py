"""Persist public asynchronous results before acknowledging the operation activity.

The execution remains a Temporal workflow. This record survives history retention
and is also the retry cache; it is never populated from an unvalidated client result.
"""
from __future__ import annotations
import json
from typing import Any
from google.protobuf import json_format, struct_pb2
from agentruntime.workflow.v1 import workspace_pb2 as flow
from agentruntime.workspace.v1 import workspace_pb2 as api
from agentruntime_core.errors import TransientError

RESPONSES = {
    flow.WORKSPACE_OPERATION_CREATE: api.CreateWorkspaceResponse,
    flow.WORKSPACE_OPERATION_COMMIT: api.CommitWorkspaceResponse,
    flow.WORKSPACE_OPERATION_PUSH: api.PushWorkspaceResponse,
    flow.WORKSPACE_OPERATION_ARCHIVE: api.ArchiveWorkspaceResponse,
    flow.WORKSPACE_OPERATION_DELETE: api.DeleteWorkspaceResponse,
    flow.WORKSPACE_OPERATION_SYNC: api.SyncWorkspaceResponse,
    flow.WORKSPACE_OPERATION_DISCARD: api.DiscardChangesResponse,
    flow.WORKSPACE_OPERATION_ATTACH_REPO: api.AttachRepoResponse,
    flow.WORKSPACE_OPERATION_DETACH_REPO: api.DetachRepoResponse,
}


def enabled(inp: Any) -> bool:
    return bool(inp.args.fields.get("__async_result")) and inp.args.fields["__async_result"].bool_value


async def cached(ops: Any, inp: Any) -> Any | None:
    if not enabled(inp):
        return None
    async with ops._archiver._db(inp.tenant.tenant_id).transaction(read_only=True) as tx:
        row = await tx.query_one("SELECT result FROM workspace_operations WHERE operation_id = $1 AND requested_by = $2 AND workspace_id = $3",
                                 (inp.operation_id, inp.requested_by, inp.workspace_id))
    if not row or not row["result"]:
        return None
    saved = json_format.Parse(row["result"], api.GetWorkspaceOperationResponse())
    return flow.WorkspaceOperationResult(error=saved.error) if saved.HasField("error") else flow.WorkspaceOperationResult(result=saved.result)


async def save(ops: Any, inp: Any, result: Any) -> Any:
    if not enabled(inp):
        return result
    envelope = api.GetWorkspaceOperationResponse(operation_id=inp.operation_id)
    if result.HasField("error"):
        envelope.status = api.WORKSPACE_OPERATION_STATUS_FAILED
        envelope.error.CopyFrom(result.error)
    else:
        public = json_format.ParseDict(json_format.MessageToDict(result.result), RESPONSES[inp.operation](), ignore_unknown_fields=True)
        envelope.status = api.WORKSPACE_OPERATION_STATUS_COMPLETED
        envelope.result.CopyFrom(json_format.ParseDict(json_format.MessageToDict(public), struct_pb2.Struct()))
    raw = json_format.MessageToJson(envelope)
    async with ops._archiver._db(inp.tenant.tenant_id).transaction() as tx:
        changed = await tx.execute("UPDATE workspace_operations SET result = $1 WHERE operation_id = $2 AND requested_by = $3 AND workspace_id = $4",
                                   (raw, inp.operation_id, inp.requested_by, inp.workspace_id))
    if not changed:
        raise TransientError("workspace_operation_unregistered", "异步操作登记暂时不可用")
    return result
