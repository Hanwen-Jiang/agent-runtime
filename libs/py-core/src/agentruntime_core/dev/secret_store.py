"""本地文件 SecretStore（开发实现）。语义见 agentruntime_core.providers.secret_store。

文件格式与 Go 侧（libs/go-core/dev/filesecrets）相同：
    {"version": 1, "scopes": {"<租户 ID 或 _platform>": {"secrets": {名字: 值}, "roles": [角色], "kek": base64}}}
kek 是该作用域的密钥加密密钥，首次生成数据密钥时创建并写回文件。短期凭据的租约只在内存里。
文件里是明文，只能用于开发。
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agentruntime_core.clock import Clock, SystemClock
from agentruntime_core.dev import _keywrap
from agentruntime_core.dev._common import PLATFORM_ID, scope_id
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError
from agentruntime_core.providers.secret_store import Credential, DataKey, LeaseStatus
from agentruntime_core.secrets import Secret
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector


@dataclass
class _Lease:
    owner: str
    role: str
    expires_at: datetime
    revoked: bool = False


class LocalFileSecretStoreProvider:
    def __init__(
        self,
        path: Path,
        *,
        max_ttl: timedelta = timedelta(hours=1),
        clock: Clock | None = None,
        faults: FaultInjector | None = None,
    ) -> None:
        self._path = Path(path)
        self._max_ttl = max_ttl
        self._clock = clock or SystemClock()
        self._faults = faults or FaultInjector()
        self._leases: dict[str, _Lease] = {}
        self._data: dict[str, Any] = (
            json.loads(self._path.read_text("utf-8")) if self._path.exists() else {"version": 1, "scopes": {}}
        )

    def bind(self, tenant: TenantContext) -> LocalFileSecretStore:
        return LocalFileSecretStore(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> LocalFileSecretStore:
        return LocalFileSecretStore(self, platform_scope(purpose))

    async def aclose(self) -> None:
        return None

    # --- 开发专用：写入静态密钥与授权角色 ---

    def seed_secret(self, tenant_id: str | None, name: str, value: str) -> None:
        self._scope(tenant_id or PLATFORM_ID)["secrets"][name] = value
        self._save()

    def grant_role(self, tenant_id: str | None, role: str) -> None:
        roles = self._scope(tenant_id or PLATFORM_ID)["roles"]
        if role not in roles:
            roles.append(role)
            self._save()

    def _scope(self, owner: str) -> dict[str, Any]:
        return self._data["scopes"].setdefault(owner, {"secrets": {}, "roles": []})

    def _kek(self, owner: str) -> bytes:
        entry = self._scope(owner)
        if "kek" not in entry:
            entry["kek"] = base64.b64encode(os.urandom(32)).decode()
            self._save()
        return base64.b64decode(entry["kek"])

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, self._path)


class LocalFileSecretStore:
    def __init__(self, provider: LocalFileSecretStoreProvider, scope: Scope) -> None:
        self._provider = provider
        self._owner = scope_id(scope)

    def _enter(self, operation: str) -> None:
        self._provider._faults.check(operation)

    async def read(self, name: str) -> Secret:
        self._enter("read")
        value = self._provider._scope(self._owner)["secrets"].get(name)
        if value is None:
            raise NotFoundError("not_found", "密钥不存在")
        return Secret(value)

    async def issue_credential(self, role: str, *, ttl: timedelta) -> Credential:
        self._enter("issue_credential")
        if role not in self._provider._scope(self._owner)["roles"]:
            raise DeniedError("role_not_granted", "该角色没有授予本作用域")
        if ttl <= timedelta(0) or ttl > self._provider._max_ttl:
            raise InvalidError("invalid_ttl", "ttl 必须大于 0 且不超过上限")
        lease_id = "lease-" + secrets.token_hex(12)
        expires_at = self._provider._clock.now() + ttl
        self._provider._leases[lease_id] = _Lease(owner=self._owner, role=role, expires_at=expires_at)
        return Credential(lease_id=lease_id, role=role, secret=Secret("dev-" + secrets.token_urlsafe(24)), expires_at=expires_at)

    def _lease(self, lease_id: str) -> _Lease:
        lease = self._provider._leases.get(lease_id)
        if lease is None or lease.owner != self._owner:
            raise NotFoundError("not_found", "租约不存在")
        return lease

    async def lease_status(self, lease_id: str) -> LeaseStatus:
        self._enter("lease_status")
        lease = self._lease(lease_id)
        if lease.revoked:
            return LeaseStatus.REVOKED
        if lease.expires_at <= self._provider._clock.now():
            return LeaseStatus.EXPIRED
        return LeaseStatus.ACTIVE

    async def revoke(self, lease_id: str) -> None:
        self._enter("revoke")
        self._lease(lease_id).revoked = True

    async def generate_data_key(self, purpose: str) -> DataKey:
        self._enter("generate_data_key")
        if not purpose:
            raise InvalidError("invalid_argument", "purpose 不能为空")
        plaintext = os.urandom(32)
        wrapped = _keywrap.wrap(self._provider._kek(self._owner), self._owner, purpose, plaintext)
        return DataKey(key_id="dk-" + secrets.token_hex(8), plaintext=Secret(plaintext), wrapped=wrapped)

    async def unwrap_data_key(self, wrapped: bytes, *, purpose: str) -> Secret:
        self._enter("unwrap_data_key")
        return Secret(_keywrap.unwrap(self._provider._kek(self._owner), self._owner, purpose, bytes(wrapped)))
