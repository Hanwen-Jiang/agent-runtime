"""Sandbox 的 Cloudflare 实现（docs/plan/cloud-deploy.md D-07）：经桥（deploy/cloudflare/sandbox-bridge，一个 Cloudflare Worker）
在 Cloudflare Containers 里建环境。每个环境一台 Firecracker microVM，容器里跑 sandbox-agent；命令、读写文件经桥转给 agent
（adapters.sandbox_agent）。语义见 agentruntime_core.providers.sandbox。只用标准库。

- 认证：桥的令牌（SecretStore 平台作用域的 sandbox/cloudflare-bridge-token）放 Authorization；作用域（租户 ID 或 _platform）
  放 X-Sandbox-Owner，桥在建环境时记下、之后每次核对，不对就是 not_found。agent 的令牌只在桥里，适配器拿不到。
- 网络：none 时出网全断；allowlist 时只放行 allow_hosts（主机名，可以用 *. 开头；只有 80、443 与 DNS）。这是 K8s 后端做不到的。
- 磁盘是临时的：空闲超过 idle_timeout 后环境被忘掉（之后访问是 not_found）。绑定 workspace 要等 D-06（工作区跟着沙箱走）：
  workspace_id 非空时报 invalid（unsupported_workspace）。
- 资源：Cloudflare 按实例类型给 CPU 与内存（桥的 wrangler.jsonc 里配），spec 的 cpu_millis、memory_mb 不再细分。
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import timedelta

from agentruntime_core.adapters.sandbox_agent import AgentClient, AgentEndpoint, check_path
from agentruntime_core.dev._common import scope_id
from agentruntime_core.errors import InvalidError, NotFoundError, TransientError
from agentruntime_core.providers.sandbox import ExecResult, SandboxEnv, SandboxSpec
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector

_ENV_ID = re.compile(r"^cfs-[0-9a-f]{16}$")
_HOST = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
MAX_HOSTS = 50


class CloudflareSandboxProvider:
    def __init__(self, bridge_url: str, bridge_token: str, *, faults: FaultInjector | None = None, agent: AgentClient | None = None) -> None:
        if not bridge_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("桥的地址要是 https（本机测试除外）")
        if len(bridge_token) < 32:
            raise ValueError("桥的令牌至少 32 个字符")
        self.bridge_url = bridge_url.rstrip("/")
        self._token = bridge_token
        self._faults = faults or FaultInjector()
        self._agent = agent or AgentClient()
        self._destroyed: set[tuple[str, str]] = set()

    def bind(self, tenant: TenantContext) -> CloudflareSandbox:
        return CloudflareSandbox(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> CloudflareSandbox:
        return CloudflareSandbox(self, platform_scope(purpose))

    async def aclose(self) -> None:
        return None

    def headers(self, owner: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "X-Sandbox-Owner": owner}

    async def call(self, method: str, path: str, owner: str, body: dict | None = None) -> tuple[int, bytes]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.bridge_url + path, data=data, method=method)
        for k, v in self.headers(owner).items():
            request.add_header(k, v)
        if data is not None:
            request.add_header("Content-Type", "application/json")

        def send() -> tuple[int, bytes]:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                # 建环境要等容器起来，冷启动通常 1 到 3 秒，留足余量。
                with opener.open(request, timeout=120) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as err:
                return err.code, err.read()
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
                raise TransientError("sandbox_unavailable", f"连不上 Cloudflare 的沙箱桥（{type(err).__name__}）") from None

        return await asyncio.to_thread(send)


class CloudflareSandbox:
    def __init__(self, provider: CloudflareSandboxProvider, scope: Scope) -> None:
        self._p = provider
        self._owner = scope_id(scope)

    def _endpoint(self, env_id: str) -> AgentEndpoint:
        if not isinstance(env_id, str) or not _ENV_ID.match(env_id) or (self._owner, env_id) in self._p._destroyed:
            raise NotFoundError("unknown_env", "环境不存在")
        # agent 的令牌在桥里加上；这里的 X-Sandbox-Token 留空，桥会换掉。
        return AgentEndpoint(f"{self._p.bridge_url}/v1/sandboxes/{env_id}/agent", "", headers=self._p.headers(self._owner))

    async def create(self, spec: SandboxSpec) -> SandboxEnv:
        self._p._faults.check("create")
        if spec.runtime != "python":
            raise InvalidError("unsupported_runtime", "Cloudflare 后端的镜像只有 python")
        if spec.workspace_id:
            raise InvalidError("unsupported_workspace", "Cloudflare 后端要等工作区跟着沙箱走（D-06）才能绑定 workspace")
        hosts: list[str] = []
        if spec.network == "allowlist":
            hosts = [h.lower() for h in spec.allow_hosts]
            if not hosts or len(hosts) > MAX_HOSTS or not all(_HOST.match(h) for h in hosts):
                raise InvalidError("invalid_allow_hosts", "allow_hosts 要是 1 到 50 个主机名（可以用 *. 开头）")
        elif spec.network != "none":
            raise InvalidError("invalid_argument", "network 只能是 none 或 allowlist")
        idle = int(spec.idle_timeout.total_seconds())
        if not 1 <= idle <= 86400:
            raise InvalidError("invalid_argument", "idle_timeout 要在 1 秒到 1 天之间")
        status, body = await self._p.call("POST", "/v1/sandboxes", self._owner, {"idle_seconds": idle, "allow_hosts": hosts})
        AgentClient.raise_for(status, body)
        try:
            env_id = str(json.loads(body)["env_id"])
        except (ValueError, KeyError, TypeError):
            raise TransientError("sandbox_bad_response", "桥返回的结果格式不对") from None
        if not _ENV_ID.match(env_id):
            raise TransientError("sandbox_bad_response", "桥返回的环境 id 格式不对")
        return SandboxEnv(env_id=env_id, spec=spec)

    async def exec(self, env_id: str, argv: Sequence[str], *, timeout: timedelta, stdin: bytes = b"") -> ExecResult:
        self._p._faults.check("exec")
        return await self._p._agent.exec(self._endpoint(env_id), argv, timeout=timeout, stdin=stdin)

    async def write_file(self, env_id: str, path: str, data: bytes) -> None:
        self._p._faults.check("write_file")
        check_path(path)
        await self._p._agent.write_file(self._endpoint(env_id), path, data)

    async def read_file(self, env_id: str, path: str) -> bytes:
        self._p._faults.check("read_file")
        check_path(path)
        return await self._p._agent.read_file(self._endpoint(env_id), path)

    async def destroy(self, env_id: str) -> None:
        self._p._faults.check("destroy")
        if not isinstance(env_id, str) or not _ENV_ID.match(env_id):
            raise NotFoundError("unknown_env", "环境不存在")
        if (self._owner, env_id) in self._p._destroyed:
            return
        status, body = await self._p.call("DELETE", f"/v1/sandboxes/{env_id}", self._owner)
        AgentClient.raise_for(status, body)
        self._p._destroyed.add((self._owner, env_id))
