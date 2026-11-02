"""N-18: tenant/task-bound ObjectStore file delivery."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agentruntime.common.v1.tenant_pb2 import TenantContext
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError
from agentruntime_core.providers.object_store import LifecycleRule
from agentruntime_core.testing import FakeClock
from agentruntime_worker.delivery import FileDelivery


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def provider(tmp_path: Path, clock: FakeClock) -> LocalDiskObjectStoreProvider:
    return LocalDiskObjectStoreProvider(tmp_path, signing_key=b"file-delivery-test", clock=clock)


def tenant(name: str) -> TenantContext:
    return TenantContext(tenant_id=name)


@pytest.mark.asyncio
async def test_publish_binds_tenant_and_task_and_authorize_checks_metadata(provider: LocalDiskObjectStoreProvider) -> None:
    delivery = FileDelivery(provider, default_ttl=timedelta(minutes=5))
    grant = await delivery.publish(tenant("tenant-a"), "tsk_123", "report.txt", b"hello", content_type="text/plain")

    assert grant.tenant_id == "tenant-a"
    assert grant.task_id == "tsk_123"
    assert grant.name == "report.txt"
    assert grant.uri == "obj://tenant-a/" + grant.uri.split("/", 3)[3]
    assert "/deliveries/tsk_123/" in grant.uri
    assert grant.size == 5
    assert grant.content_type == "text/plain"

    await delivery.authorize(tenant("tenant-a"), "tsk_123", grant)
    with pytest.raises(DeniedError):
        await delivery.authorize(tenant("tenant-a"), "tsk_other", grant)
    with pytest.raises(DeniedError):
        await delivery.authorize(tenant("tenant-b"), "tsk_123", grant)


@pytest.mark.asyncio
async def test_publish_rejects_path_injection_and_non_positive_ttl(provider: LocalDiskObjectStoreProvider) -> None:
    delivery = FileDelivery(provider)
    with pytest.raises(InvalidError):
        await delivery.publish(tenant("tenant-a"), "tsk/123", "report.txt", b"x")
    with pytest.raises(InvalidError):
        await delivery.publish(tenant("tenant-a"), "tsk_123", "../report.txt", b"x")
    with pytest.raises(InvalidError):
        await delivery.publish(tenant("tenant-a"), "tsk_123", "report.txt", b"x", ttl=timedelta(0))


@pytest.mark.asyncio
async def test_object_store_lifecycle_removes_expired_delivery(provider: LocalDiskObjectStoreProvider, clock: FakeClock) -> None:
    delivery = FileDelivery(provider, default_ttl=timedelta(minutes=5))
    grant = await delivery.publish(tenant("tenant-a"), "tsk_123", "report.txt", b"hello")
    store = provider.bind(tenant("tenant-a"))
    await store.set_lifecycle([LifecycleRule(prefix="deliveries/", expire_after=timedelta(minutes=5))])
    await delivery.authorize(tenant("tenant-a"), "tsk_123", grant)

    clock.advance(timedelta(minutes=5))
    with pytest.raises(DeniedError):
        await delivery.authorize(tenant("tenant-a"), "tsk_123", grant)
    with pytest.raises(NotFoundError):
        await store.head(store.key_of(grant.uri))

