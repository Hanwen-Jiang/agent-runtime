"""Short-lived task file delivery on top of the tenant-bound ObjectStore.

This module only issues a claim (the object URI and a temporary GET URL).  A
public download handler can call :meth:`FileDelivery.authorize` before
returning or proxying the URL.  The object store itself remains the authority
for tenant isolation and URL expiry; lifecycle rules are configured by the
deployment and are deliberately not replaced here.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from agentruntime_core.errors import DeniedError, InvalidError, ProviderError
from agentruntime_core.providers.object_store import ObjectStore, ObjectStoreProvider
from agentruntime_core.tenancy import TenantContext

_DELIVERY_PREFIX: Final = "deliveries"
_DEFAULT_TTL: Final = timedelta(minutes=15)
_MAX_COMPONENT_LENGTH: Final = 255


@dataclass(frozen=True)
class FileGrant:
    """A file claim that is scoped to one tenant and one task."""

    tenant_id: str
    task_id: str
    name: str
    uri: str
    download_url: str
    expires_at: datetime
    size: int
    sha256: str
    content_type: str


def _component(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_COMPONENT_LENGTH:
        raise InvalidError("invalid_file_delivery", f"{field} 必须是 1 到 {_MAX_COMPONENT_LENGTH} 个字符")
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise InvalidError("invalid_file_delivery", f"{field} 必须是单个路径段")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise InvalidError("invalid_file_delivery", f"{field} 不得含控制字符")
    return value


def _ttl(value: timedelta | None) -> timedelta:
    result = _DEFAULT_TTL if value is None else value
    if result <= timedelta(0):
        raise InvalidError("invalid_file_delivery", "ttl 必须大于 0")
    return result


class FileDelivery:
    """Publish and validate task file grants without introducing new state."""

    def __init__(self, objects: ObjectStoreProvider, *, default_ttl: timedelta = _DEFAULT_TTL) -> None:
        self._objects = objects
        self._default_ttl = _ttl(default_ttl)

    async def publish(
        self,
        tenant: TenantContext,
        task_id: str,
        name: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        ttl: timedelta | None = None,
    ) -> FileGrant:
        """Store one file and issue a short-lived GET URL.

        The object key contains the task id and a random nonce.  ObjectStore
        ``bind`` supplies the tenant scope, so both parts of the scope are
        checked by the existing provider rather than by a second registry.
        """

        task_id = _component(task_id, field="task_id")
        name = _component(name, field="name")
        if not isinstance(data, bytes):
            raise InvalidError("invalid_file_delivery", "data 必须是 bytes")
        if not isinstance(content_type, str) or not content_type or any(ord(c) < 0x20 or ord(c) == 0x7F for c in content_type):
            raise InvalidError("invalid_file_delivery", "content_type 不合法")
        lifetime = self._default_ttl if ttl is None else _ttl(ttl)

        store = self._objects.bind(tenant)
        # URL-safe token characters are valid ObjectStore key characters.
        key = f"{_DELIVERY_PREFIX}/{task_id}/{secrets.token_urlsafe(18)}/{name}"
        info = await store.put(
            key,
            data,
            content_type=content_type,
            metadata={"task_id": task_id, "file_name": name},
            if_absent=True,
        )
        url = await store.temporary_url(key, ttl=lifetime, method="GET")
        return FileGrant(
            tenant_id=tenant.tenant_id,
            task_id=task_id,
            name=name,
            uri=info.uri,
            download_url=url,
            expires_at=info.created_at + lifetime,
            size=info.size,
            sha256=info.sha256,
            content_type=info.content_type,
        )

    async def authorize(self, tenant: TenantContext, task_id: str, grant: FileGrant) -> None:
        """Check tenant/task scope against the authoritative object metadata.

        This is intentionally a read-only check for a future download
        endpoint.  It does not mint a second token or copy object state.
        """

        task_id = _component(task_id, field="task_id")
        if not isinstance(grant, FileGrant) or grant.task_id != task_id:
            raise DeniedError("file_delivery_scope", "文件链接不属于当前租户或任务")

        store: ObjectStore = self._objects.bind(tenant)
        if grant.tenant_id != tenant.tenant_id:
            raise DeniedError("file_delivery_scope", "文件链接不属于当前租户或任务")
        try:
            key = store.key_of(grant.uri)
            info = await store.head(key)
        except ProviderError as error:
            # Provider implementations use Denied/NotFound for cross-scope or
            # missing objects.  Keep the download surface from leaking either
            # distinction.
            raise DeniedError("file_delivery_scope", "文件链接不可用") from error
        expected_prefix = f"{_DELIVERY_PREFIX}/{task_id}/"
        if not key.startswith(expected_prefix) or info.metadata.get("task_id") != task_id or info.metadata.get("file_name") != grant.name:
            raise DeniedError("file_delivery_scope", "文件链接不属于当前任务")

