from types import SimpleNamespace
import json
import pytest
from google.protobuf import json_format, struct_pb2
from agentruntime.workflow.v1 import workspace_pb2 as flow
from agentruntime.common.v1 import tenant_pb2, error_pb2
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.providers.relational_store import Migration
from agentruntime_core.tenancy import TenantContext
from agentruntime_worker.workspace import async_results

@pytest.mark.parametrize("failed", [False, True])
async def test_activity_persists_public_result_without_polling_and_reuses_it(tmp_path, failed):
    store=SQLiteRelationalStoreProvider(tmp_path)
    await store.migrate((Migration(212,"workspace_async_operations",(
        "CREATE TABLE workspace_operations (operation_id TEXT PRIMARY KEY, requested_by TEXT NOT NULL, workspace_id TEXT NOT NULL, input_digest TEXT NOT NULL, response_type TEXT NOT NULL, result TEXT NOT NULL DEFAULT '')",
    )),))
    tenant=TenantContext(tenant_id="t1")
    db=store.bind(tenant)
    async with db.transaction() as tx:
        await tx.execute("INSERT INTO workspace_operations (operation_id, requested_by, workspace_id, input_digest, response_type) VALUES ($1,$2,$3,$4,$5)",("wso-test","alice","w1","digest","response"))
    ops=SimpleNamespace(_archiver=SimpleNamespace(_db=lambda _:db))
    inp=flow.WorkspaceOperationInput(tenant=tenant_pb2.TenantContext(tenant_id="t1"),operation_id="wso-test",requested_by="alice",workspace_id="w1",operation=flow.WORKSPACE_OPERATION_CREATE,args=json_format.ParseDict({"__async_result":True},struct_pb2.Struct()))
    assert await async_results.cached(ops,inp) is None
    result=flow.WorkspaceOperationResult(error=error_pb2.Error(category=error_pb2.ERROR_CATEGORY_DENIED,code="repo_read_only",message="read only")) if failed else flow.WorkspaceOperationResult(result=json_format.ParseDict({"workspace":{"workspace_id":"w1"},"sealed_credential":"DO-NOT-PERSIST"},struct_pb2.Struct()))
    await async_results.save(ops,inp,result)
    async with db.transaction(read_only=True) as tx:
        saved=await tx.query_one("SELECT result FROM workspace_operations WHERE operation_id = $1",("wso-test",))
    assert "DO-NOT-PERSIST" not in saved["result"]
    assert json.loads(saved["result"])["status"] == ("WORKSPACE_OPERATION_STATUS_FAILED" if failed else "WORKSPACE_OPERATION_STATUS_COMPLETED")
    cached=await async_results.cached(ops,inp)
    assert cached.HasField("error") == failed
    if not failed:
        assert json_format.MessageToDict(cached.result)["workspace"]["workspaceId"] == "w1"
    await store.aclose()
