"""租户策略里的沙箱部分（tenant_policy 的 sandbox；W4、W5、W10、W11；D-08、D-12、D-13）。

    {"sandbox": {"backend": "kata", "fallback": ["cloudflare"], "min_isolation": "microvm",
                 "idle_timeout_s": 600, "max_lifetime_s": 7200,
                 "quota": {"max_concurrent": 4, "daily_sandbox_seconds": 36000, "daily_cost_usd": 5, ...},
                 "network": {...}}}

- backend 没写时用部署的默认后端；fallback 是暂时不可用时依次试的后端；min_isolation 没写时取首选后端的隔离强度。
- idle_timeout_s、max_lifetime_s 只能调低部署的默认值（15 分钟、6 小时），写得更高按默认值算。
- 由策略决定，模型与请求都不能指定。
- 顶层的 workspace（D-06）：{"max_workspaces": 10, "max_bytes": 2147483648, "push_branches": ["agent/*"], "cold_after_days": 14}，
  见 WorkspaceLimits。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from agentruntime_worker.sandbox.ledger import Quota

ISOLATION_RANK = {"none": 0, "container": 1, "microvm": 2}
DEFAULT_IDLE = timedelta(minutes=15)
DEFAULT_MAX_LIFETIME = timedelta(hours=6)


@dataclass(frozen=True)
class WorkspaceLimits:
    """租户策略的 workspace 一节（设计书改动稿 W2；D-06，docs/plan/workspace-layer.md）：同时存在的工作区个数、单个工作区
    （存档）的字节上限、能推送的远端分支（glob：* 匹配不含 / 的一段，** 匹配任意多段）。不写的项用部署的默认值。"""

    max_workspaces: int = 20
    max_bytes: int = 2 << 30
    push_branches: tuple[str, ...] = ("agent/*",)
    # 冷热分层（DISK-B）：挂卷的工作区闲置超过这么多天转冷；0 表示租户没写，用部署的默认值（AR_WORKSPACE_COLD_AFTER_DAYS）。
    cold_after_days: int = 0

    @classmethod
    def from_policy(cls, doc: Mapping[str, Any] | None) -> WorkspaceLimits:
        section = dict(((doc or {}).get("workspace") or {}))
        default = cls()
        return cls(
            max_workspaces=int(section.get("max_workspaces") or default.max_workspaces),
            max_bytes=int(section["max_bytes"]) if section.get("max_bytes") is not None else default.max_bytes,
            push_branches=tuple(str(b) for b in section.get("push_branches") or default.push_branches),
            cold_after_days=int(section.get("cold_after_days") or default.cold_after_days),
        )


@dataclass(frozen=True)
class SandboxPolicy:
    backend: str
    fallback: tuple[str, ...] = ()
    min_isolation: str = ""
    quota: Quota = field(default_factory=Quota)
    idle_timeout: timedelta = DEFAULT_IDLE
    max_lifetime: timedelta = DEFAULT_MAX_LIFETIME
    network: Mapping[str, Any] = field(default_factory=dict)
    # 不在 sandbox 一节里，是租户策略顶层的 workspace（D-06）；跟着沙箱策略一起按租户缓存。
    workspace: WorkspaceLimits = field(default_factory=WorkspaceLimits)

    @classmethod
    def from_document(
        cls,
        doc: Mapping[str, Any] | None,
        default_backend: str,
        *,
        idle_default: timedelta = DEFAULT_IDLE,
        max_default: timedelta = DEFAULT_MAX_LIFETIME,
    ) -> SandboxPolicy:
        sandbox = dict(((doc or {}).get("sandbox") or {}))
        idle = idle_default
        if sandbox.get("idle_timeout_s"):
            idle = min(idle_default, timedelta(seconds=int(sandbox["idle_timeout_s"])))
        lifetime = max_default
        if sandbox.get("max_lifetime_s"):
            lifetime = min(max_default, timedelta(seconds=int(sandbox["max_lifetime_s"])))
        return cls(
            backend=str(sandbox.get("backend") or default_backend),
            fallback=tuple(str(b) for b in sandbox.get("fallback") or ()),
            min_isolation=str(sandbox.get("min_isolation") or ""),
            quota=Quota.from_policy(sandbox.get("quota")),
            idle_timeout=idle,
            max_lifetime=lifetime,
            network=dict(sandbox.get("network") or {}),
            workspace=WorkspaceLimits.from_policy(doc),
        )

    @property
    def names(self) -> tuple[str, ...]:
        """首选加回退，去重保序。"""
        out: list[str] = []
        for name in (self.backend, *self.fallback):
            if name not in out:
                out.append(name)
        return tuple(out)
