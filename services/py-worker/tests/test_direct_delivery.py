"""WK-LITE-02: reservations count before upload and failure releases only its own reservation."""
import asyncio
import hashlib
import pytest
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.errors import InvalidError
from agentruntime_core.tenancy import TenantContext
from agentruntime_worker.tools import deliver
from .test_deliver_file import Env, TENANT, TASK

class DirectReader:
    def __init__(self, env):
        self.env = env
        self.prepared = asyncio.Event()
        self.continue_upload = asyncio.Event()
        self.uploads = 0
        self.fail = False

    async def read(self, *args, **kwargs):
        raise AssertionError("Worker must not read direct upload bytes")

    async def upload_invocation(self, invocation, path, name, reserve):
        data = b"agent bytes"
        await reserve(len(data))
        self.prepared.set()
        await self.continue_upload.wait()
        if self.fail:
            raise InvalidError("upload_failed", "injected")
        self.uploads += 1
        key = f"deliveries/{TASK}/{invocation.idempotency_key}/{name}"
        store = self.env.objects.bind(TenantContext(tenant_id=TENANT))
        info = await store.put(key, data, if_absent=True)
        return {"object_key": key, "object_uri": info.uri, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

def driver(env, reader):
    out = LocalToolDriverProvider()
    deliver.register_delivery_tools(out, objects=env.objects, store=env.store, reader=reader, limits=env.limits, clock=env.clock)
    return out

async def test_pending_upload_consumes_quota_and_completed_retry_is_idempotent(tmp_path):
    env = Env(tmp_path, deliver.DeliveryLimits(max_file_bytes=20, max_files_per_task=1, max_task_bytes=20))
    reader = DirectReader(env); tools = driver(env, reader)
    first = asyncio.create_task(env.call({"path": "out.bin"}, "one", tools))
    await asyncio.wait_for(reader.prepared.wait(), 5)
    with pytest.raises(InvalidError, match="最多交付"):
        await env.call({"path": "out.bin"}, "two", tools)
    with pytest.raises(InvalidError, match="正在上传"):
        await env.call({"path": "out.bin"}, "one", driver(env, reader))
    assert reader.uploads == 0
    reader.continue_upload.set()
    result = await first
    repeated = await env.call({"path": "out.bin"}, "one", tools)
    assert repeated.output == result.output and reader.uploads == 1
    async with env.store.bind(TenantContext(tenant_id=TENANT)).transaction(read_only=True) as tx:
        assert await tx.query("SELECT * FROM task_artifact_reservations") == []

async def test_upload_failure_releases_reservation_and_retry_works(tmp_path):
    env = Env(tmp_path, deliver.DeliveryLimits(max_file_bytes=20, max_files_per_task=1, max_task_bytes=20))
    reader = DirectReader(env); reader.continue_upload.set(); reader.fail = True
    tools = driver(env, reader)
    with pytest.raises(InvalidError, match="injected"):
        await env.call({"path": "out.bin"}, "same", tools)
    reader.fail = False
    result = await env.call({"path": "out.bin"}, "same", tools)
    assert result.output["size_bytes"] == len(b"agent bytes")
    assert reader.uploads == 1
