"""沙箱环境的短期凭据与注入的环境变量（docs/plan/sandbox-platform.md 第 3 节；D-15 包镜像、D-16 出网网关）。

每个环境一个凭据（用途 mirror），到期时间等于环境的活满时间；包镜像据此知道是哪个租户、按租户策略的
daily_mirror_miss_bytes 限制未命中。凭据只经 exec 的 env 给到这一条命令，不写进 Pod 的定义、环境表与 trace。

注入的变量（指向包镜像，凭据在路径前缀 /t/<凭据>/ 里，pip、npm、go 都不用另配认证）：
    PIP_INDEX_URL、UV_DEFAULT_INDEX    <镜像>/t/<凭据>/pypi/simple/
    PIP_TRUSTED_HOST                   镜像是 http 时（集群内）
    npm_config_registry                <镜像>/t/<凭据>/npm/（另关掉 audit 与 fund：断网时它们只会报错）
    GOPROXY                            <镜像>/t/<凭据>/go（校验数据库也经它代理）
哪些后端注入：登记里写了 pkg_mirror_url 的用它；没写的 K8s 与本地后端用 Worker 的 AR_PKG_MIRROR_URL；托管后端
（Cloudflare 等）默认不注入（断网时连不到集群里的镜像，要给它们配经公网入口的 pkg_mirror_url）。
"""

from __future__ import annotations

import asyncio
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from agentruntime_core import envcred

from agentruntime_worker.sandbox.ledger import EnvRecord, Quota, parse_iso

KeyLoader = Callable[[], Awaitable[bytes | None]]


class EnvCredentials:
    def __init__(self, load_key: KeyLoader, *, mirror_url: str = "") -> None:
        self._load_key = load_key
        self._key: bytes | None = None
        self._lock = asyncio.Lock()
        self.mirror_url = mirror_url.rstrip("/")

    async def key(self) -> bytes | None:
        if self._key is None:
            async with self._lock:
                if self._key is None:
                    self._key = await self._load_key()
        return self._key

    def mirror_for(self, spec: Mapping[str, Any]) -> str:
        """这个后端用的包镜像地址；空表示不注入。"""
        if spec.get("pkg_mirror_url"):
            return str(spec["pkg_mirror_url"]).rstrip("/")
        if spec.get("kind") in ("k8s", "local"):
            return self.mirror_url
        return ""

    async def exec_env(self, record: EnvRecord, backend_spec: Mapping[str, Any], quota: Quota) -> dict[str, str]:
        mirror = self.mirror_for(backend_spec)
        if not mirror:
            return {}
        key = await self.key()
        if not key:
            return {}
        expires = parse_iso(record.max_until) or (datetime.now(UTC) + timedelta(hours=6))
        limits = {"mirror_miss_bytes": quota.daily_mirror_miss_bytes} if quota.daily_mirror_miss_bytes else {}
        token = envcred.issue(key, envcred.Claims(
            env_ref=record.env_ref, tenant_id=record.owner, workspace_id=record.workspace_id, session_id=record.session_id,
            expires_at=int(expires.timestamp()), purpose="mirror", limits=limits,
        ))
        return mirror_env(mirror, token)


def mirror_env(mirror: str, token: str) -> dict[str, str]:
    base = f"{mirror}/t/{token}"
    env = {
        "PIP_INDEX_URL": f"{base}/pypi/simple/",
        "UV_DEFAULT_INDEX": f"{base}/pypi/simple/",
        "npm_config_registry": f"{base}/npm/",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
        "GOPROXY": f"{base}/go",
    }
    parsed = urllib.parse.urlsplit(mirror)
    if parsed.scheme == "http" and parsed.hostname:
        env["PIP_TRUSTED_HOST"] = parsed.hostname
    return env


__all__ = ["EnvCredentials", "mirror_env"]
