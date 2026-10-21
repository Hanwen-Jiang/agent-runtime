"""MemoryService 的 gRPC 服务与客户端（proto agentruntime.memory.v1）。

认证在服务端：metadata 的 Bearer 令牌 → 身份；请求的 tenant 只用来核对。客户端按租户签短期令牌，
submit_candidates 可以直接替换 Worker 收尾写回用的 RecordingMemoryClient。
"""

from __future__ import annotations

import json

import grpc
import pytest
from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import payload_pb2, tenant_pb2
from agentruntime.memory.v1 import memory_pb2, memory_pb2_grpc

from agentruntime_memory.client import MemoryServiceClient
from agentruntime_memory.identity import DevTokenAuthenticator, issue_dev_token
from agentruntime_memory.server import start_server

from .memory_support import ALICE, BOB, TASK_ID, Backend, item

KEY = b"memory-service-test-key"


def payload(data: dict) -> payload_pb2.JsonPayload:
    value = struct_pb2.Value()
    json_format.ParseDict(data, value)
    return payload_pb2.JsonPayload(schema_id="https://agentruntime.example/schemas/memory_item.schema.json", value=value)


def token_for(tenant: tenant_pb2.TenantContext) -> str:
    return issue_dev_token(KEY, tenant=tenant.tenant_id, user=tenant.user_id, workspace=tenant.workspace_id, session=tenant.session_id)


@pytest.fixture
async def server(dev: Backend):
    srv, port = await start_server(dev.service, DevTokenAuthenticator(KEY), "127.0.0.1:0")
    yield f"127.0.0.1:{port}"
    await srv.stop(grace=None)


async def test_submit_search_feedback_and_delete_through_the_client(server: str, dev: Backend) -> None:
    client = MemoryServiceClient(server, token_for)
    try:
        entry = item()
        sub = await client.submit_candidates(
            memory_pb2.SubmitCandidatesRequest(tenant=ALICE.tenant_context(), candidates=[memory_pb2.MemoryCandidate(memory_item=payload(entry), source_task_id=TASK_ID)])
        )
        assert sub.accepted == 1
        await dev.service.drain(ALICE)
        found = await client.search(memory_pb2.SearchRequest(tenant=ALICE.tenant_context(), trigger=memory_pb2.SEARCH_TRIGGER_PLANNING, query="redis make test", top_k=5))
        assert [json_format.MessageToDict(i.memory_item.value)["id"] for i in found.items] == [entry["id"]]
        await client.record_feedback(
            memory_pb2.RecordFeedbackRequest(tenant=ALICE.tenant_context(), task_id=TASK_ID, feedback=[memory_pb2.Feedback(memory_id=entry["id"], adopted=True, task_succeeded=True)])
        )
        deleted = await client.delete(memory_pb2.DeleteRequest(tenant=ALICE.tenant_context(), memory_id=entry["id"], reason="不再需要"))
        assert list(deleted.deleted_memory_ids) == [entry["id"]]
    finally:
        await client.aclose()


async def raw_call(address: str, metadata, request: memory_pb2.SearchRequest) -> grpc.aio.AioRpcError:
    async with grpc.aio.insecure_channel(address) as channel:
        stub = memory_pb2_grpc.MemoryServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as err:
            await stub.Search(request, metadata=metadata)
    return err.value


async def test_calls_without_a_valid_token_are_unauthenticated(server: str) -> None:
    request = memory_pb2.SearchRequest(tenant=ALICE.tenant_context(), trigger=memory_pb2.SEARCH_TRIGGER_PLANNING, query="x")
    assert (await raw_call(server, (), request)).code() == grpc.StatusCode.UNAUTHENTICATED
    assert (await raw_call(server, (("authorization", "Bearer forged.token"),), request)).code() == grpc.StatusCode.UNAUTHENTICATED


async def test_a_token_for_one_tenant_cannot_act_for_another(server: str) -> None:
    alice_token = token_for(ALICE.tenant_context())
    request = memory_pb2.SearchRequest(tenant=BOB.tenant_context(), trigger=memory_pb2.SEARCH_TRIGGER_PLANNING, query="x")
    err = await raw_call(server, (("authorization", f"Bearer {alice_token}"),), request)
    assert err.code() == grpc.StatusCode.PERMISSION_DENIED
    missing = memory_pb2.SearchRequest(trigger=memory_pb2.SEARCH_TRIGGER_PLANNING, query="x")
    assert (await raw_call(server, (("authorization", f"Bearer {alice_token}"),), missing)).code() == grpc.StatusCode.PERMISSION_DENIED


async def test_error_messages_do_not_echo_item_contents(server: str) -> None:
    client = MemoryServiceClient(server, token_for)
    try:
        bad = {**item(guidance="密钥 password=hunter2-correct-horse"), "kind": "rumor"}
        sub = await client.submit_candidates(
            memory_pb2.SubmitCandidatesRequest(tenant=ALICE.tenant_context(), candidates=[memory_pb2.MemoryCandidate(memory_item=payload(bad), source_task_id=TASK_ID)])
        )
        assert sub.accepted == 0
    finally:
        await client.aclose()
    assert "hunter2" not in json.dumps(str(sub))
