"""本地文件 SecretStore（开发实现）。语义见 agentruntime_core.providers.secret_store。

文件格式与 Go 侧（libs/go-core/dev/filesecrets）相同：
    {"version": 1, "scopes": {"<租户 ID 或 _platform>": {"secrets": {名字: 值}, "roles": [角色], "kek": base64}}}
kek 是该作用域的密钥加密密钥，首次生成数据密钥时创建并写回文件。短期凭据的租约只在内存里。
多个进程可以共用一份文件（task-service 与 Worker 互相解开对方的 Claim-Check 数据密钥，S-06）：
写文件时加锁并先重新读，缺 kek 时先重新读文件再生成，不会覆盖别的进程写进去的内容。
文件里是明文，只能用于开发与单节点演示。

集群里（docs/plan/cloud-deploy.md D-04）静态密钥来自只读挂载的 K8s Secret，kek 又要写回：密钥文件放在共用的可写卷上，
各进程启动时用 merge_seed 把只读的种子文件并进来（AR_SECRETS_SEED_FILE）。Go 侧 filesecrets.MergeSeed 规则相同。
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agentruntime_core.clock import Clock, SystemClock
from agentruntime_core.dev import _keywrap
from agentruntime_core.dev._common import PLATFORM_ID, scope_id
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError, TransientError
from agentruntime_core.providers.secret_store import Credential, DataKey, LeaseStatus
from agentruntime_core.secrets import Secret
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector

# 跨进程文件锁的等待上限，以及判定为崩溃遗留锁的时长。
_LOCK_TIMEOUT_S = 10.0
_LOCK_STALE_S = 30.0


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
        with self._locked():
            self._scope(tenant_id or PLATFORM_ID)["secrets"][name] = value
            self._save()

    def grant_role(self, tenant_id: str | None, role: str) -> None:
        with self._locked():
            roles = self._scope(tenant_id or PLATFORM_ID)["roles"]
            if role not in roles:
                roles.append(role)
                self._save()

    def merge_seed(self, seed: Path) -> None:
        """把种子文件（同样的格式，通常是只读挂载的 K8s Secret）并进密钥文件：种子里的密钥覆盖同名的（轮换时改种子），
        角色取并集；kek 只在本文件还没有时才取种子的，已有的不换（用它包过的数据密钥要能解开）。"""
        try:
            data = json.loads(Path(seed).read_text("utf-8"))
        except (OSError, ValueError) as err:
            raise InvalidError("invalid_seed", f"读不了密钥的种子文件（{type(err).__name__}）") from None
        scopes = data.get("scopes") if isinstance(data, dict) else None
        if not isinstance(scopes, dict):
            raise InvalidError("invalid_seed", "密钥的种子文件里没有 scopes")
        with self._locked():
            for owner, entry in scopes.items():
                mine = self._scope(owner)
                mine["secrets"].update(entry.get("secrets") or {})
                mine["roles"] = sorted(set(mine["roles"]) | set(entry.get("roles") or []))
                if "kek" not in mine and entry.get("kek"):
                    mine["kek"] = entry["kek"]
            self._save()

    def _scope(self, owner: str) -> dict[str, Any]:
        return self._data["scopes"].setdefault(owner, {"secrets": {}, "roles": []})

    def _kek(self, owner: str) -> bytes:
        """作用域的密钥加密密钥。别的进程可能已经写进文件（S-06：task-service 与 Worker 共用一份）：
        内存里没有就加锁、重新读文件，还没有才生成并写回，不会覆盖别人的。"""
        entry = self._scope(owner)
        if "kek" not in entry:
            with self._locked():
                entry = self._scope(owner)
                if "kek" not in entry:
                    entry["kek"] = base64.b64encode(os.urandom(32)).decode()
                    self._save()
        return base64.b64decode(entry["kek"])

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """跨进程的文件锁（<文件>.lock，独占创建）；拿到锁后重新读文件，修改基于最新内容。"""
        lock = self._path.with_name(self._path.name + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                with contextlib.suppress(OSError):
                    if time.time() - lock.stat().st_mtime > _LOCK_STALE_S:
                        lock.unlink()  # 持锁的进程崩溃留下的锁
                        continue
                if time.monotonic() > deadline:
                    raise TransientError("busy", "密钥文件被别的进程锁住") from None
                time.sleep(0.01)
        try:
            if self._path.exists():
                self._data = json.loads(self._path.read_text("utf-8"))
            yield
        finally:
            os.close(fd)
            with contextlib.suppress(OSError):
                lock.unlink()

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
