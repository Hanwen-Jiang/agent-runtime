"""Sandbox 的自托管 E2B 实现（e2b 分支，docs/plan/e2b-backend.md）。语义见 agentruntime_core.providers.sandbox。只用标准库。

E2B 的开源运行时（github.com/e2b-dev/runtime）在专用的 KVM 节点上跑，每个环境一台 Firecracker microVM：
- 生命周期经 E2B 的 api（REST，X-API-Key）：POST /v2/sandboxes 建、GET /sandboxes/{id} 查、DELETE 删、
  POST /sandboxes/{id}/timeout 顺延、POST /sandboxes/{id}/pause 暂停（内存与进程都留下）、POST /v2/sandboxes/{id}/connect
  恢复、GET /v2/sandboxes 列出（给沙箱环境表对账）。
- 命令与文件直接经沙箱里的 envd（adapters.envd），不在模板里放 sandbox-agent：模板的启动命令在构建时就跑起来并进了快照，
  每个环境的令牌与环境变量带不进去；envd 的每个进程可以带 envs，令牌由 E2B 签发。

作用域：E2B 只有一个 team（部署共用的 API key），租户隔离由这里做：metadata 记着所属作用域（agentruntime.owner）与部署
（agentruntime.deployment），每次访问先核对（进程内缓存；换了 Worker 副本时经 GET /sandboxes/{id} 再查），不对就是 not_found。
envd 的令牌只在内存里，不进日志、trace 与错误信息。

- 网络：none 时 allow_internet_access=false；allowlist 时 network.allowOut 放行 allow_hosts（可以用 *. 开头），其余
  denyOut 0.0.0.0/0。沙箱的端口一律要 E2B 的 traffic 令牌（allowPublicTraffic=false）。配了 egress_proxy 时
  network.egressProxy 指到出网网关（SOCKS5；凭据由 token_key 对环境派生），出网的审计与字节上限归网关（main 的 D-16）。
- 资源：CPU 与内存由模板定（构建时给 cpuCount、memoryMB）；spec 的 cpu_millis、memory_mb 只校验，不再细分。
- 空闲：E2B 的 timeout 就是空闲期限，每次用时顺延（同一个环境最多每分钟一次）；pause_on_idle 时到期暂停而不是删掉
  （autoPause），下次用时 connect 恢复，内存与进程都在（会话级沙箱的快速路径，main 的 D-13）。
- CPU 时间：envd 不报，cpu_time 为 None。
- 绑定 workspace 要等工作区能存档与恢复（main 的 D-06）：workspace_id 非空时 invalid（unsupported_workspace）。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from agentruntime_core.adapters.envd import EnvdClient, EnvdEndpoint, header_routed, host_routed
from agentruntime_core.adapters.sandbox_agent import check_path
from agentruntime_core.dev._common import scope_id
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError, TransientError
from agentruntime_core.providers.sandbox import ExecResult, SandboxEnv, SandboxSpec
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector

OWNER_KEY = "agentruntime.owner"
DEPLOYMENT_KEY = "agentruntime.deployment"
IDLE_KEY = "agentruntime.idle_seconds"
EGRESS_USER_KEY = "agentruntime.egress_user"
_ENV_ID = re.compile(r"^e2b-([a-z0-9]{1,64})$")
_SANDBOX_ID = re.compile(r"^[a-z0-9]{1,64}$")
_HOST = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
_DEPLOYMENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
MAX_HOSTS = 50
TOUCH_INTERVAL = 60.0


@dataclass(frozen=True)
class E2BSandboxConfig:
    # E2B 的 api（Embed 是 http://<节点>:3000）与 client-proxy（http://<节点>:3002）。
    api_url: str
    sandbox_url: str = ""
    # header：经 client-proxy 按头路由（自托管的默认）；host：按 https://<端口>-<id>.<域名>（api 返回的 domain）。
    routing: str = "header"
    # runtime → 模板名（scripts/e2b_template.py 构建，与 K8s 的镜像同一个 Dockerfile）。
    templates: Mapping[str, str] = field(default_factory=lambda: {"python": "agentruntime-python"})
    # 部署的标签：列出与对账时只看自己部署的沙箱（同一个 E2B 可能被几套部署共用）。
    deployment: str = "default"
    workdir: str = "/home/user/workspace"
    user: str = "user"
    # 每条命令都带的环境变量（包镜像的地址等，main 的 D-15）；不放密钥。
    envs: Mapping[str, str] = field(default_factory=dict)
    # 出网网关的 SOCKS5 地址（host:port）；空表示不经网关。
    egress_proxy: str = ""
    pause_on_idle: bool = False
    request_timeout: timedelta = timedelta(seconds=60)
    # 建沙箱要等 VM 从快照恢复，通常一秒以内，留足余量。
    create_timeout: timedelta = timedelta(seconds=120)


@dataclass
class _Known:
    owner: str
    sandbox_id: str
    envd_token: str
    domain: str
    idle_seconds: int
    touched: float = 0.0


@dataclass(frozen=True)
class ListedEnv:
    env_id: str
    owner: str
    state: str
    started_at: datetime | None


class E2BSandboxProvider:
    def __init__(
        self,
        config: E2BSandboxConfig,
        api_key: str,
        *,
        token_key: bytes | None = None,
        faults: FaultInjector | None = None,
        envd: EnvdClient | None = None,
    ) -> None:
        for name, url in (("api_url", config.api_url), ("sandbox_url", config.sandbox_url)):
            if url and not url.startswith(("https://", "http://")):
                raise ValueError(f"{name} 要是 http(s) 地址")
        if not config.api_url:
            raise ValueError("要给 E2B 的 api_url")
        if config.routing not in ("header", "host"):
            raise ValueError("routing 只能是 header 或 host")
        if config.routing == "header" and not config.sandbox_url:
            raise ValueError("按头路由时要给 sandbox_url（E2B 的 client-proxy）")
        if not _DEPLOYMENT.match(config.deployment):
            raise ValueError("deployment 的格式不对")
        if not config.workdir.startswith("/") or ".." in config.workdir.split("/"):
            raise ValueError("workdir 要是绝对路径")
        if len(api_key) < 20:
            raise ValueError("E2B 的 API key 太短")
        self.config = config
        self._api_key = api_key
        self._token_key = token_key or secrets.token_bytes(32)
        self._faults = faults or FaultInjector()
        self._envd = envd or EnvdClient(request_timeout=config.request_timeout)
        self._known: dict[str, _Known] = {}
        self._destroyed: dict[str, str] = {}

    def bind(self, tenant: TenantContext) -> E2BSandbox:
        return E2BSandbox(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> E2BSandbox:
        return E2BSandbox(self, platform_scope(purpose))

    async def aclose(self) -> None:
        return None

    # --- 给沙箱环境表对账（main 的 D-11）---

    async def list_environments(self) -> list[ListedEnv]:
        """本部署在 E2B 上的全部沙箱（running 与 paused）。按部署标签过滤，别的部署的不碰。"""
        out: list[ListedEnv] = []
        token = ""
        for _ in range(1000):
            query = {"metadata": f"{_q(DEPLOYMENT_KEY)}={_q(self.config.deployment)}", "state": "running,paused", "limit": "100"}
            if token:
                query["nextToken"] = token
            status, headers, body = await self.api("GET", "/v2/sandboxes?" + urllib.parse.urlencode(query))
            _raise_api(status, body)
            for item in _json_list(body):
                sandbox_id = str(item.get("sandboxID") or "")
                metadata = item.get("metadata") or {}
                if not _SANDBOX_ID.match(sandbox_id) or metadata.get(DEPLOYMENT_KEY) != self.config.deployment:
                    continue
                out.append(ListedEnv("e2b-" + sandbox_id, str(metadata.get(OWNER_KEY) or ""), str(item.get("state") or ""), _parse_time(item.get("startedAt"))))
            token = headers.get("X-Next-Token", "")
            if not token:
                break
        return out

    # --- 内部 ---

    def egress_credentials(self, user: str) -> str:
        return hmac.new(self._token_key, b"egress:" + user.encode(), hashlib.sha256).hexdigest()

    def endpoint(self, known: _Known) -> EnvdEndpoint:
        c = self.config
        if c.routing == "host":
            if not known.domain:
                raise TransientError("sandbox_bad_response", "E2B 没有给出沙箱的域名")
            return host_routed(known.domain, known.sandbox_id, known.envd_token, user=c.user)
        return header_routed(c.sandbox_url, known.sandbox_id, known.envd_token, user=c.user)

    async def api(
        self, method: str, path: str, body: Mapping[str, Any] | None = None, *, idempotency_key: str = "", timeout: timedelta | None = None
    ) -> tuple[int, Mapping[str, str], bytes]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.config.api_url.rstrip("/") + path, data=data, method=method)
        request.add_header("X-API-Key", self._api_key)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if idempotency_key:
            request.add_header("Idempotency-Key", idempotency_key)
        limit = (timeout or self.config.request_timeout).total_seconds()

        def send() -> tuple[int, Mapping[str, str], bytes]:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(request, timeout=limit) as resp:
                    return resp.status, dict(resp.headers.items()), resp.read()
            except urllib.error.HTTPError as err:
                return err.code, dict(err.headers.items()) if err.headers else {}, err.read()
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
                raise TransientError("sandbox_unavailable", f"连不上 E2B（{type(err).__name__}）") from None

        return await asyncio.to_thread(send)


class E2BSandbox:
    def __init__(self, provider: E2BSandboxProvider, scope: Scope) -> None:
        self._p = provider
        self._owner = scope_id(scope)

    async def create(self, spec: SandboxSpec) -> SandboxEnv:
        p, c = self._p, self._p.config
        p._faults.check("create")
        template = c.templates.get(spec.runtime)
        if not template:
            raise InvalidError("unsupported_runtime", f"没有配置 runtime {spec.runtime} 的 E2B 模板")
        if spec.workspace_id:
            raise InvalidError("unsupported_workspace", "E2B 后端要等工作区能存档与恢复（D-06）才能绑定 workspace")
        if spec.cpu_millis <= 0 or spec.memory_mb <= 0:
            raise InvalidError("invalid_argument", "cpu_millis、memory_mb 都要大于 0")
        idle = int(spec.idle_timeout.total_seconds())
        if not 1 <= idle <= 86400:
            raise InvalidError("invalid_argument", "idle_timeout 要在 1 秒到 1 天之间")
        network: dict[str, Any] = {"allowPublicTraffic": False}
        body: dict[str, Any] = {"templateID": template, "timeout": idle, "autoPause": c.pause_on_idle}
        if spec.network == "allowlist":
            hosts = [h.lower() for h in spec.allow_hosts]
            if not hosts or len(hosts) > MAX_HOSTS or not all(_HOST.match(h) for h in hosts):
                raise InvalidError("invalid_allow_hosts", "allow_hosts 要是 1 到 50 个主机名（可以用 *. 开头）")
            network["allowOut"] = hosts
            network["denyOut"] = ["0.0.0.0/0"]
        elif spec.network == "none":
            body["allow_internet_access"] = False
        else:
            raise InvalidError("invalid_argument", "network 只能是 none 或 allowlist")
        key = secrets.token_hex(16)
        metadata = {OWNER_KEY: self._owner, DEPLOYMENT_KEY: c.deployment, IDLE_KEY: str(idle)}
        if c.egress_proxy:
            user = "e2b-" + key
            network["egressProxy"] = {"address": c.egress_proxy, "username": user, "password": p.egress_credentials(user)}
            metadata[EGRESS_USER_KEY] = user
        body["network"] = network
        body["metadata"] = metadata
        status, _, data = await self._create_call(body, key)
        _raise_api(status, data, creating=True)
        created = _json_obj(data)
        sandbox_id = str(created.get("sandboxID") or "")
        token = str(created.get("envdAccessToken") or "")
        if not _SANDBOX_ID.match(sandbox_id) or not token:
            raise TransientError("sandbox_bad_response", "E2B 返回的沙箱格式不对")
        env_id = "e2b-" + sandbox_id
        known = _Known(self._owner, sandbox_id, token, str(created.get("domain") or ""), idle, time.monotonic())
        try:
            # 建工作目录（envd 写文件时会建父目录）；同时确认 envd 已经能用。
            await p._envd.write_file(p.endpoint(known), f"{c.workdir}/.agentruntime", b"")
        except BaseException:
            await self._kill(sandbox_id)
            raise
        p._known[env_id] = known
        return SandboxEnv(env_id=env_id, spec=spec)

    async def exec(self, env_id: str, argv: Sequence[str], *, timeout: timedelta, stdin: bytes = b"") -> ExecResult:
        self._p._faults.check("exec")
        known = await self._lookup(env_id)
        if not argv:
            raise InvalidError("invalid_argument", "argv 不能为空")
        await self._touch(env_id, known)
        c = self._p.config
        return await self._p._envd.run(self._p.endpoint(known), list(argv), cwd=c.workdir, envs=dict(c.envs), timeout=timeout, stdin=stdin)

    async def write_file(self, env_id: str, path: str, data: bytes) -> None:
        self._p._faults.check("write_file")
        check_path(path)
        known = await self._lookup(env_id)
        await self._p._envd.write_file(self._p.endpoint(known), f"{self._p.config.workdir}/{path}", data)

    async def read_file(self, env_id: str, path: str) -> bytes:
        self._p._faults.check("read_file")
        check_path(path)
        known = await self._lookup(env_id)
        try:
            return await self._p._envd.read_file(self._p.endpoint(known), f"{self._p.config.workdir}/{path}")
        except InvalidError as err:
            # envd 对目录报 400；按接口语义，读不到文件是 not_found。
            raise NotFoundError("file_not_found", "沙箱里没有这个文件") from err

    async def destroy(self, env_id: str) -> None:
        p = self._p
        p._faults.check("destroy")
        sandbox_id = _sandbox_id(env_id)
        if p._destroyed.get(env_id) == self._owner:
            return
        status, _, data = await p.api("GET", f"/sandboxes/{sandbox_id}")
        if status == 404:
            p._known.pop(env_id, None)
            return  # 已经没了：重复删除不报错
        _raise_api(status, data)
        if not self._mine(_json_obj(data)):
            raise NotFoundError("unknown_env", "环境不存在")
        await self._kill(sandbox_id)
        p._destroyed[env_id] = self._owner
        p._known.pop(env_id, None)

    async def pause(self, env_id: str) -> None:
        """暂停：内存、进程与文件都留下，不占 CPU；下次用时自动恢复（main 的 D-13 用）。"""
        known = await self._lookup(env_id)
        status, _, data = await self._p.api("POST", f"/sandboxes/{known.sandbox_id}/pause", {"memory": True})
        if status != 409:  # 409：已经暂停了
            _raise_api(status, data)

    # --- 内部 ---

    async def _create_call(self, body: Mapping[str, Any], key: str) -> tuple[int, Mapping[str, str], bytes]:
        """连不上时用同一个 Idempotency-Key 再试一次：E2B 会重放第一次的结果，不会多建。"""
        timeout = self._p.config.create_timeout
        try:
            return await self._p.api("POST", "/v2/sandboxes", body, idempotency_key=key, timeout=timeout)
        except TransientError:
            await asyncio.sleep(0.5)
            return await self._p.api("POST", "/v2/sandboxes", body, idempotency_key=key, timeout=timeout)

    async def _kill(self, sandbox_id: str) -> None:
        status, _, data = await self._p.api("DELETE", f"/sandboxes/{sandbox_id}")
        if status not in (204, 404):
            _raise_api(status, data)

    def _mine(self, detail: Mapping[str, Any]) -> bool:
        metadata = detail.get("metadata") or {}
        return metadata.get(OWNER_KEY) == self._owner and metadata.get(DEPLOYMENT_KEY) == self._p.config.deployment

    async def _lookup(self, env_id: str) -> _Known:
        p = self._p
        sandbox_id = _sandbox_id(env_id)
        if env_id in p._destroyed:
            raise NotFoundError("unknown_env", "环境不存在")
        known = p._known.get(env_id)
        if known is not None:
            if known.owner != self._owner:
                raise NotFoundError("unknown_env", "环境不存在")
            return known
        # 别的 Worker 副本建的（或进程重启过）：问 E2B，核对作用域；暂停的先恢复。
        status, _, data = await p.api("GET", f"/sandboxes/{sandbox_id}")
        if status == 404:
            raise NotFoundError("unknown_env", "环境不存在")
        _raise_api(status, data)
        detail = _json_obj(data)
        if not self._mine(detail):
            raise NotFoundError("unknown_env", "环境不存在")
        idle = _int((detail.get("metadata") or {}).get(IDLE_KEY), 600)
        token = str(detail.get("envdAccessToken") or "")
        domain = str(detail.get("domain") or "")
        if detail.get("state") == "paused" or not token:
            status, _, data = await p.api("POST", f"/v2/sandboxes/{sandbox_id}/connect", {"timeout": idle}, timeout=p.config.create_timeout)
            if status == 404:
                raise NotFoundError("unknown_env", "环境不存在")
            _raise_api(status, data)
            resumed = _json_obj(data)
            token = str(resumed.get("envdAccessToken") or token)
            domain = str(resumed.get("domain") or domain)
        if not token:
            raise TransientError("sandbox_bad_response", "E2B 没有给出沙箱的访问令牌")
        known = _Known(self._owner, sandbox_id, token, domain, idle, time.monotonic())
        p._known[env_id] = known
        return known

    async def _touch(self, env_id: str, known: _Known) -> None:
        """顺延空闲期限；一分钟内用过就不再顺延。"""
        now = time.monotonic()
        if now - known.touched < TOUCH_INTERVAL:
            return
        status, _, data = await self._p.api("POST", f"/sandboxes/{known.sandbox_id}/timeout", {"timeout": known.idle_seconds})
        if status == 404:
            self._p._known.pop(env_id, None)
            raise NotFoundError("unknown_env", "环境不存在")
        _raise_api(status, data)
        known.touched = now


def _sandbox_id(env_id: str) -> str:
    m = _ENV_ID.match(env_id) if isinstance(env_id, str) else None
    if not m:
        raise NotFoundError("unknown_env", "环境不存在")
    return m.group(1)


def _raise_api(status: int, body: bytes, *, creating: bool = False) -> None:
    """E2B api 的错误：{"code": int, "message": str}。消息里没有我们的密钥；只截一段。"""
    if 200 <= status < 300:
        return
    message = ""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            message = str(data.get("message") or "")[:300]
    except ValueError:
        pass
    if status in (400, 422):
        raise InvalidError("sandbox_rejected", message or "E2B 拒绝了这个请求")
    if status in (401, 403):
        raise DeniedError("sandbox_denied", "E2B 拒绝了 API key（配错了或已吊销）")
    if status == 404:
        if creating:
            raise InvalidError("unknown_template", message or "E2B 里没有这个模板")
        raise NotFoundError("unknown_env", "环境不存在")
    if status == 429:
        # 并发上限或频率限制：容量满，按暂时性处理（可以回退到下一个后端，main 的 D-12）。
        raise TransientError("sandbox_capacity", message or "E2B 的并发或频率到了上限")
    raise TransientError("sandbox_unavailable", message or f"E2B 暂时不可用（HTTP {status}）")


def _json_obj(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError:
        raise TransientError("sandbox_bad_response", "E2B 返回的结果格式不对") from None
    if not isinstance(data, dict):
        raise TransientError("sandbox_bad_response", "E2B 返回的结果格式不对")
    return data


def _json_list(body: bytes) -> list[dict[str, Any]]:
    try:
        data = json.loads(body)
    except ValueError:
        raise TransientError("sandbox_bad_response", "E2B 返回的结果格式不对") from None
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


__all__ = ["E2BSandbox", "E2BSandboxConfig", "E2BSandboxProvider", "ListedEnv"]
