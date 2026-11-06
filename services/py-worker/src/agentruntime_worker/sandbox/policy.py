"""Worker 还要读的租户策略（SBX-01 之后）。选后端、配额、空闲与活满都在 Sandbox Service 里；Worker 只读：

- 顶层的 workspace（D-06）：{"max_workspaces": 10, "max_bytes": 2147483648, "push_branches": ["agent/*"], "cold_after_days": 14}，
  Workspace 操作建工作区、推送时核对（WorkspaceLimits）。
- sandbox.network（D-16）：审批联网时核对（request_network、Workspace 操作连远端；规则在 sandbox.grants）。

策略经 Registry 读当前版本（registry_policy），按租户缓存一分钟（TenantPolicies）；没配 Registry 时都用默认值。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agentruntime_core.errors import NotFoundError

# 读一个租户的当前策略文档（没有策略时 None）。
PolicyLookup = Callable[[str], Awaitable[Mapping[str, Any] | None]]


@dataclass(frozen=True)
class WorkspaceLimits:
    """租户策略的 workspace 一节（设计书改动稿 W2；D-06，docs/plan/workspace-layer.md）：同时存在的工作区个数、单个工作区
    （存档）的字节上限、能推送的远端分支（glob：* 匹配不含 / 的一段，** 匹配任意多段）。不写的项用部署的默认值。"""

    max_repos: int = 10
    max_workspaces: int = 20
    max_bytes: int = 2 << 30
    push_branches: tuple[str, ...] = ("agent/*",)
    cold_after_days: int = 0

    @classmethod
    def from_policy(cls, doc: Mapping[str, Any] | None) -> WorkspaceLimits:
        section = dict(((doc or {}).get("workspace") or {}))
        default = cls()
        return cls(
            max_repos=int(section.get("max_repos") or default.max_repos),
            max_workspaces=int(section.get("max_workspaces") or default.max_workspaces),
            max_bytes=int(section["max_bytes"]) if section.get("max_bytes") is not None else default.max_bytes,
            push_branches=tuple(str(b) for b in section.get("push_branches") or default.push_branches),
            cold_after_days=int(section.get("cold_after_days") or default.cold_after_days),
        )


@dataclass(frozen=True)
class TenantPolicy:
    workspace: WorkspaceLimits
    network: Mapping[str, Any]


class TenantPolicies:
    """按租户缓存的策略（cache_ttl 秒）。"""

    def __init__(self, lookup: PolicyLookup | None, *, cache_ttl: float = 60) -> None:
        self._lookup = lookup
        self._ttl = cache_ttl
        self._cache: dict[str, tuple[float, TenantPolicy]] = {}

    async def policy_for(self, tenant_id: str) -> TenantPolicy:
        cached = self._cache.get(tenant_id)
        if cached and time.monotonic() - cached[0] < self._ttl:
            return cached[1]
        doc: Mapping[str, Any] | None = None
        if self._lookup is not None:
            try:
                doc = await self._lookup(tenant_id)
            except NotFoundError:
                doc = None
        sandbox = dict(((doc or {}).get("sandbox") or {}))
        policy = TenantPolicy(workspace=WorkspaceLimits.from_policy(doc), network=dict(sandbox.get("network") or {}))
        self._cache[tenant_id] = (time.monotonic(), policy)
        return policy


def registry_policy(address: str) -> PolicyLookup:
    """经 Registry 读租户的当前策略文档。"""
    from google.protobuf import json_format

    from agentruntime_worker.activities.types import Tenant
    from agentruntime_worker.registry.client import GrpcRegistry

    registry = GrpcRegistry(address)

    async def lookup(tenant_id: str) -> Mapping[str, Any] | None:
        policy = await registry.get_tenant_policy(Tenant(tenant_id=tenant_id))
        if not policy.HasField("document"):
            return None
        value = json_format.MessageToDict(policy.document.value)
        return value if isinstance(value, dict) else None

    return lookup


__all__ = ["PolicyLookup", "TenantPolicies", "TenantPolicy", "WorkspaceLimits", "registry_policy"]
