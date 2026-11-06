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

class CommitAckFaultStore:
    """Commit the real SQLite transaction, then lose its acknowledgement once."""
    def __init__(self, inner, *, lose_lookup=False):
        self.inner = inner
        self.lost = False
        self.lose_lookup = lose_lookup

    async def migrate(self, migrations):
        return await self.inner.migrate(migrations)

    def bind(self, tenant):
        from contextlib import asynccontextmanager
        bound = self.inner.bind(tenant)
        owner = self

        class Bound:
            @asynccontextmanager
            async def transaction(self, *args, **kwargs):
                wrote_artifact = False
                async with bound.transaction(*args, **kwargs) as tx:
                    class Tx:
                        def __getattr__(self, name):
                            return getattr(tx, name)

                        async def execute(self, sql, params=()):
                            nonlocal wrote_artifact
                            wrote_artifact |= sql.startswith("INSERT INTO task_artifacts ")
                            return await tx.execute(sql, params)

                        async def query_one(self, sql, params=()):
                            if owner.lost and owner.lose_lookup and sql.startswith("SELECT * FROM task_artifacts"):
                                raise InvalidError("lookup_unavailable", "cannot resolve commit")
                            return await tx.query_one(sql, params)
                    yield Tx()
                if wrote_artifact and not owner.lost:
                    owner.lost = True
                    raise InvalidError("commit_ack_lost", "commit acknowledgement lost")
        return Bound()


async def test_commit_ack_loss_does_not_delete_registered_delivery(tmp_path):
    env = Env(tmp_path)
    original_store = env.store
    env.store = CommitAckFaultStore(original_store)
    reader = DirectReader(env)
    reader.continue_upload.set()
    tools = driver(env, reader)
    result = await env.call({"path": "out.bin"}, "lost-ack", tools)
    async with original_store.bind(TenantContext(tenant_id=TENANT)).transaction(read_only=True) as tx:
        row = await tx.query_one("SELECT * FROM task_artifacts WHERE idempotency_key=$1", ("lost-ack",))
    bucket = env.objects.bind(TenantContext(tenant_id=TENANT))
    assert row is not None and (await bucket.get(bucket.key_of(row["object_uri"]))).data == b"agent bytes"
    assert result.output["artifact_id"] == row["artifact_id"]
    repeated = await env.call({"path": "out.bin"}, "lost-ack", driver(env, reader))
    assert repeated.output == result.output and reader.uploads == 1


async def test_unknown_commit_outcome_keeps_object_and_original_error(tmp_path):
    env = Env(tmp_path)
    original_store = env.store
    env.store = CommitAckFaultStore(original_store, lose_lookup=True)
    reader = DirectReader(env)
    reader.continue_upload.set()
    with pytest.raises(InvalidError, match="commit acknowledgement lost"):
        await env.call({"path": "out.bin"}, "unknown", driver(env, reader))
    async with original_store.bind(TenantContext(tenant_id=TENANT)).transaction(read_only=True) as tx:
        row = await tx.query_one("SELECT * FROM task_artifacts WHERE idempotency_key=$1", ("unknown",))
    bucket = env.objects.bind(TenantContext(tenant_id=TENANT))
    assert row is not None and (await bucket.get(bucket.key_of(row["object_uri"]))).data == b"agent bytes"
    env.store.lose_lookup = False
    recovered = await env.call({"path": "out.bin"}, "unknown", driver(env, reader))
    assert recovered.output["artifact_id"] == row["artifact_id"] and reader.uploads == 1


async def test_cleanup_failure_keeps_reservation_and_original_validation_error(tmp_path):
    env = Env(tmp_path)
    reader = DirectReader(env)
    reader.continue_upload.set()
    original_upload = reader.upload_invocation

    async def invalid_size(*args):
        ref = await original_upload(*args)
        ref["size_bytes"] += 1
        return ref
    reader.upload_invocation = invalid_size
    original_objects = env.objects

    class Objects:
        def bind(self, tenant):
            inner = original_objects.bind(tenant)
            class Bound:
                def __getattr__(self, key):
                    return getattr(inner, key)
                async def delete(self, key):
                    raise InvalidError("cleanup_unavailable", "deletion unavailable")
            return Bound()
    env.objects = Objects()
    with pytest.raises(InvalidError, match="预占配额不一致"):
        await env.call({"path": "out.bin"}, "cleanup-fail", driver(env, reader))
    async with env.store.bind(TenantContext(tenant_id=TENANT)).transaction(read_only=True) as tx:
        assert len(await tx.query("SELECT * FROM task_artifact_reservations")) == 1
        assert await tx.query("SELECT * FROM task_artifacts") == []
