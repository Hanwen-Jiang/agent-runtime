"""沙箱环境的短期凭据与注入的环境变量（docs/plan/sandbox-platform.md 第 3 节；D-15 包镜像、D-16 出网网关）。

每个环境两个凭据（用途 mirror 与 egress），到期时间等于环境的活满时间；包镜像据此知道是哪个租户、按租户策略的
daily_mirror_miss_bytes 限制未命中，出网网关据此在环境表里找到环境与正在用它的 Task、按 Task 的联网授权放行。
凭据只经 exec 的 env 给到这一条命令，不写进 Pod 的定义、环境表与 trace。

注入的变量：
    PIP_INDEX_URL、UV_DEFAULT_INDEX    <镜像>/t/<凭据>/pypi/simple/（凭据在路径前缀里，pip、npm、go 都不用另配认证）
    PIP_TRUSTED_HOST                   镜像是 http 时（集群内）
    npm_config_registry                <镜像>/t/<凭据>/npm/（另关掉 audit 与 fund：断网时它们只会报错）
    GOPROXY                            <镜像>/t/<凭据>/go（校验数据库也经它代理）
    HTTP_PROXY、HTTPS_PROXY（与小写）   http://ar:<凭据>@<出网网关>（D-16；没有授权时网关一律拒绝，所以总是注入）
    NO_PROXY（与小写）                  包镜像的主机、localhost、127.0.0.1：装依赖不经网关
    npm_config_proxy、npm_config_https_proxy  同上（npm 的代理另有配置项）
哪些后端注入：登记里写了 pkg_mirror_url、egress_gateway_url 的用它；没写的 K8s 与本地后端用 Worker 的 AR_PKG_MIRROR_URL、
AR_EGRESS_GATEWAY_URL；托管后端（Cloudflare 等）默认不注入（断网时连不到集群里的服务；联网按它们原生的域名白名单）。

测试用的密钥（W8）：租户把要注入的变量以 JSON 对象存进本租户 SecretStore 的 sandbox/test-env，Worker 每条命令都带上；
名字的规则同 exec 的 env，不能盖掉上面这些变量。日志与 trace 里只记名字（span 的 sandbox.test_env）。
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from agentruntime_core import envcred
from agentruntime_core.errors import InvalidError
from agentruntime_core.providers.sandbox import check_exec_env

from agentruntime_worker.sandbox.ledger import EnvRecord, Quota, parse_iso

KeyLoader = Callable[[], Awaitable[bytes | None]]
# 读一个租户的测试用密钥（sandbox/test-env 的值）；没有时 None。
TestEnvLoader = Callable[[str], Awaitable[bytes | None]]
TEST_ENV_SECRET = "sandbox/test-env"
_RESERVED = frozenset({
    "PIP_INDEX_URL", "UV_DEFAULT_INDEX", "PIP_TRUSTED_HOST", "NPM_CONFIG_REGISTRY", "GOPROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "ALL_PROXY", "NPM_CONFIG_PROXY", "NPM_CONFIG_HTTPS_PROXY", "NPM_CONFIG_AUDIT", "NPM_CONFIG_FUND", "GONOSUMDB", "GOFLAGS",
})


class EnvCredentials:
    def __init__(self, load_key: KeyLoader, *, mirror_url: str = "", egress_url: str = "", test_env: TestEnvLoader | None = None) -> None:
        self._load_key = load_key
        self._key: bytes | None = None
        self._lock = asyncio.Lock()
        self.mirror_url = mirror_url.rstrip("/")
        self.egress_url = egress_url.rstrip("/")
        self._test_env = test_env

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

    def egress_for(self, spec: Mapping[str, Any]) -> str:
        """这个后端用的出网网关地址；空表示没有网关（托管后端、没配网关，或登记里写了 egress_gateway: false）。"""
        if spec.get("egress_gateway") is False:
            return ""
        if spec.get("egress_gateway_url"):
            return str(spec["egress_gateway_url"]).rstrip("/")
        if spec.get("kind") in ("k8s", "local"):
            return self.egress_url
        return ""

    def _token(self, key: bytes, record: EnvRecord, purpose: str, limits: Mapping[str, int]) -> str:
        expires = parse_iso(record.max_until) or (datetime.now(UTC) + timedelta(hours=6))
        return envcred.issue(key, envcred.Claims(
            env_ref=record.env_ref, tenant_id=record.owner, workspace_id=record.workspace_id, session_id=record.session_id,
            expires_at=int(expires.timestamp()), purpose=purpose, limits=dict(limits),
        ))

    async def exec_env(self, record: EnvRecord, backend_spec: Mapping[str, Any], quota: Quota) -> dict[str, str]:
        out: dict[str, str] = {}
        mirror, egress = self.mirror_for(backend_spec), self.egress_for(backend_spec)
        key = await self.key() if (mirror or egress) else None
        if key:
            if mirror:
                limits = {"mirror_miss_bytes": quota.daily_mirror_miss_bytes} if quota.daily_mirror_miss_bytes else {}
                out.update(mirror_env(mirror, self._token(key, record, "mirror", limits)))
            if egress:
                limits = {"egress_bytes": quota.daily_egress_bytes} if quota.daily_egress_bytes else {}
                out.update(proxy_env(egress, self._token(key, record, "egress", limits), mirror))
        return out

    async def test_env(self, tenant_id: str) -> dict[str, str]:
        """租户的测试用密钥（sandbox/test-env）；没有时空。格式不对 invalid（说明哪一处不对，不带值）。"""
        if self._test_env is None or not tenant_id or tenant_id == "_platform":
            return {}
        raw = await self._test_env(tenant_id)
        if not raw:
            return {}
        try:
            doc = json.loads(raw)
        except ValueError:
            raise InvalidError("invalid_test_env", f"{TEST_ENV_SECRET} 要是 JSON 对象") from None
        if not isinstance(doc, dict) or not all(isinstance(v, str) for v in doc.values()):
            raise InvalidError("invalid_test_env", f"{TEST_ENV_SECRET} 要是名字到字符串的 JSON 对象")
        env = check_exec_env(doc)
        clash = sorted(k for k in env if k.upper() in _RESERVED)
        if clash:
            raise InvalidError("invalid_test_env", f"{TEST_ENV_SECRET} 不能设这些变量：{'、'.join(clash)}")
        return env


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


def proxy_env(gateway: str, token: str, mirror: str = "") -> dict[str, str]:
    """经出网网关的代理变量；凭据是代理认证的口令（Proxy-Authorization: Basic），各家客户端都认 URL 里的 userinfo。"""
    parsed = urllib.parse.urlsplit(gateway)
    proxy = f"{parsed.scheme or 'http'}://ar:{token}@{parsed.netloc}"
    bypass = ["localhost", "127.0.0.1"]
    mirror_host = urllib.parse.urlsplit(mirror).hostname if mirror else None
    if mirror_host:
        bypass.insert(0, mirror_host)
    no_proxy = ",".join(bypass)
    return {
        "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy,
        "NO_PROXY": no_proxy, "no_proxy": no_proxy,
        "npm_config_proxy": proxy, "npm_config_https_proxy": proxy,
    }


__all__ = ["EnvCredentials", "TEST_ENV_SECRET", "mirror_env", "proxy_env"]
