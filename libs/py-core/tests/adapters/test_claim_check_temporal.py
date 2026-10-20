"""S-06 验收：Claim-Check 接入 Temporal（Python 侧，Temporal 本地开发服务）。

- 客户端按认证身份绑定租户（StaticTenant），Worker 按工作流 memo 里的作用域确定租户（WorkflowMemoResolver）；
  大载荷经工作流往返后不变，事件历史里只有引用。
- 子工作流的 ID 以父 Task 的 ID 开头（<task_id>/r<round>/<node>），按根 ID 找到租户。
- 回放工具按事件历史第一个事件的 memo 绑定租户，带引用的历史能用当前代码回放。
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from google.protobuf.wrappers_pb2 import StringValue
from temporalio.client import Client
from temporalio.converter import DataConverter, WorkflowSerializationContext
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from agentruntime_core.adapters import claim_check as cc
from agentruntime_core.adapters.temporal import SCOPE_MEMO_KEY
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.tenancy import TenantContext

from ..dev.temporal_workflows import EchoWorkflow

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

T1 = TenantContext(tenant_id="t1")
BIG = "大" * (cc.DEFAULT_THRESHOLD // 2)  # UTF-8 三字节一个字，序列化后超过阈值


def converter(claim_check: cc.ClaimCheck, resolver: cc.TenantResolver) -> DataConverter:
    return dataclasses.replace(DataConverter.default, payload_codec=claim_check.codec(resolver))


@dataclasses.dataclass
class Setup:
    raw: Client
    claim_check: cc.ClaimCheck
    task_queue: str

    def client_for(self, tenant: TenantContext) -> Client:
        """按认证身份绑定了租户的客户端（与 DurableEngine 的 bind 对应）。"""
        return Client(self.raw.service_client, namespace=self.raw.namespace, data_converter=converter(self.claim_check, cc.StaticTenant(tenant)))


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def setup(temporal_env: WorkflowEnvironment, tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Setup]:
    root: Path = tmp_path_factory.mktemp("claim-check")
    claim_check = cc.ClaimCheck(
        LocalDiskObjectStoreProvider(root / "objects", signing_key=b"claim-check-temporal"),
        LocalFileSecretStoreProvider(root / "secrets.json"),
    )
    raw = temporal_env.client
    worker_client = Client(raw.service_client, namespace=raw.namespace, data_converter=converter(claim_check, cc.WorkflowMemoResolver(raw)))
    task_queue = f"claim-check-{uuid.uuid4().hex[:8]}"
    async with Worker(worker_client, task_queue=task_queue, workflows=[EchoWorkflow]):
        yield Setup(raw, claim_check, task_queue)


async def run_echo(setup: Setup, tenant: TenantContext, workflow_id: str) -> str:
    client = setup.client_for(tenant)
    handle = await client.start_workflow(
        EchoWorkflow.run,
        StringValue(value=BIG),
        id=workflow_id,
        task_queue=setup.task_queue,
        memo={SCOPE_MEMO_KEY: tenant.tenant_id},
        result_type=StringValue,
        execution_timeout=timedelta(minutes=2),
    )
    await handle.signal("append", StringValue(value="尾"))
    await handle.signal("finish")
    return (await handle.result()).value


async def test_large_payloads_round_trip_through_a_workflow_as_references(setup: Setup) -> None:
    workflow_id = f"tsk_cc_{uuid.uuid4().hex}"
    assert await run_echo(setup, T1, workflow_id) == BIG + ",尾"

    history = await setup.raw.get_workflow_handle(workflow_id).fetch_history()
    sizes = [len(p.SerializeToString()) for e in history.events for p in _payloads(e)]
    assert sizes and max(sizes) < 4096, f"事件历史里只有引用，最大的载荷 {max(sizes)} 字节"
    keys = (await setup.claim_check.objects.bind(T1).list(prefix=cc.KEY_PREFIX)).items
    assert keys, "大载荷存进了租户 t1 的 ObjectStore"


async def test_memo_resolver_finds_the_tenant_by_the_root_workflow_id(setup: Setup) -> None:
    workflow_id = f"tsk_cc_{uuid.uuid4().hex}"
    await run_echo(setup, T1, workflow_id)
    resolver = cc.WorkflowMemoResolver(setup.raw)
    for wid in (workflow_id, f"{workflow_id}/r1/research", f"{workflow_id}/r2/fix/batch-1"):
        tenant = await resolver.tenant_for(WorkflowSerializationContext(namespace=setup.raw.namespace, workflow_id=wid))
        assert tenant is not None and tenant.tenant_id == "t1", wid
    assert await resolver.tenant_for(WorkflowSerializationContext(namespace=setup.raw.namespace, workflow_id="tsk_does_not_exist")) is None
    assert await resolver.tenant_for(None) is None


async def test_history_with_references_replays_with_the_tenant_from_its_memo(setup: Setup) -> None:
    workflow_id = f"tsk_cc_{uuid.uuid4().hex}"
    await run_echo(setup, T1, workflow_id)
    history = await setup.raw.get_workflow_handle(workflow_id).fetch_history()
    tenant = cc.tenant_from_history(history)
    assert tenant is not None and tenant.tenant_id == "t1"
    replayer = Replayer(workflows=[EchoWorkflow], data_converter=converter(setup.claim_check, cc.StaticTenant(tenant)))
    result = await replayer.replay_workflow(history, raise_on_replay_failure=False)
    assert result.replay_failure is None, result.replay_failure


def _payloads(event: object) -> list:
    name = event.WhichOneof("attributes")  # type: ignore[attr-defined]
    if name is None:
        return []
    attrs = getattr(event, name)
    out = []
    for descriptor, value in attrs.ListFields():
        if descriptor.message_type is not None and descriptor.message_type.full_name == "temporal.api.common.v1.Payloads":
            out.extend(value.payloads)
    return out
