"""shell_run 的沙箱后端（docs/plan/cloud-deploy.md D-05、D-07、D-08）。

AR_TOOL_SHELL_SANDBOX=sandbox 时 shell_run 经 Sandbox 接口跑：每条命令建一个绑定本 workspace 的环境，执行完就销毁
（与 docker 时一样是一次性的）。后端登记在 AR_SANDBOX_BACKENDS_FILE（JSON）：

    {
      "default": "gvisor",
      "backends": {
        "gvisor": {"kind": "k8s", "isolation": "container", "runtime_class": "gvisor", "namespace": "sandboxes",
                   "image": "python:3.13-slim", "agent_image": "ghcr.io/agentruntime/sandbox-agent:0.1.0",
                   "connect": "pod_ip", "workspace_claim": "workspaces"},
        "kata":   {"kind": "k8s", "isolation": "microvm", "runtime_class": "kata", ...},
        "cloudflare": {"kind": "cloudflare", "isolation": "microvm", "bridge_url": "https://sandbox.example.workers.dev"},
        "local":  {"kind": "local", "isolation": "none"}
      }
    }

- k8s：agentruntime_core.adapters.k8s_sandbox；可选 kubeconfig（JSON；不给时在集群内用 ServiceAccount）、
  max_envs_per_scope（每个租户同时最多几个环境，D-10；超了 denied）。
- cloudflare：agentruntime_core.adapters.cloudflare_sandbox；桥的令牌取自 SecretStore 平台作用域的 sandbox/cloudflare-bridge-token。
- local：本地子进程（开发实现），不隔离，只给开发与测试。
- e2b：自托管的 E2B（e2b 分支），见 tools/sandbox_e2b.py。
用哪个后端由租户策略的 sandbox.backend 决定（经 Registry 读当前版本，按租户缓存一分钟），没写时用 default；模型与请求都不能指定。
登记里没有的名字一律拒绝，不悄悄换后端。K8s 后端的 agent 令牌密钥（同一部署的 Worker 共用）取自 SecretStore 平台作用域的
sandbox/token-key；没有时每个进程随机一把（只有一个 Worker 进程时可以）。
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from agentruntime_core.errors import InvalidError, NotFoundError
from agentruntime_core.providers.sandbox import SandboxProvider

from agentruntime_worker.sandbox.policy import SandboxPolicy

KINDS = ("k8s", "cloudflare", "local", "e2b")
ISOLATIONS = ("none", "container", "microvm")
TOKEN_KEY_SECRET = "sandbox/token-key"
BRIDGE_TOKEN_SECRET = "sandbox/cloudflare-bridge-token"
SECRET_PURPOSE = "sandbox"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

# 读一个租户的当前策略文档（没有策略时 None）。
PolicyLookup = Callable[[str], Awaitable[Mapping[str, Any] | None]]


@dataclass(frozen=True)
class Backend:
    name: str
    kind: str
    isolation: str
    provider: SandboxProvider


class Sandboxes:
    """登记的沙箱后端，加上按租户选后端的规则。后端在第一次用到时才建（读密钥要 await）。"""

    def __init__(
        self,
        specs: Mapping[str, Mapping[str, Any]],
        default: str,
        *,
        workspace_root: Path | None = None,
        secrets_file: Path | None = None,
        policy: PolicyLookup | None = None,
        cache_ttl: float = 60,
        environments: Mapping[str, Any] | None = None,
    ) -> None:
        if default not in specs:
            raise ValueError(f"沙箱后端的 default {default!r} 没有登记")
        for name, spec in specs.items():
            if not _NAME.match(name):
                raise ValueError(f"沙箱后端名 {name!r} 格式不对")
            if spec.get("kind") not in KINDS:
                raise ValueError(f"沙箱后端 {name} 的 kind 只能是 {'、'.join(KINDS)}")
            if spec.get("isolation", "container") not in ISOLATIONS:
                raise ValueError(f"沙箱后端 {name} 的 isolation 只能是 {'、'.join(ISOLATIONS)}")
            if spec["kind"] == "e2b":
                from agentruntime_worker.tools import sandbox_e2b

                sandbox_e2b.check_spec(name, spec)
        self._specs = dict(specs)
        self.default = default
        self._workspace_root = workspace_root
        self._secrets_file = secrets_file
        self._policy = policy
        self._cache_ttl = cache_ttl
        self._backends: dict[str, Backend] = {}
        self._policies: dict[str, tuple[float, SandboxPolicy]] = {}
        self._lock = asyncio.Lock()
        # 登记文件里的 environments 一节（按项目定制的运行环境，D-17；格式见 tools/__init__.py 的 environment_manager）。
        self.environments: dict[str, Any] = dict(environments or {})

    @classmethod
    def from_file(cls, path: Path, **kwargs: Any) -> Sandboxes:
        try:
            doc = json.loads(Path(path).read_text("utf-8"))
        except (OSError, ValueError) as err:
            raise ValueError(f"读不了沙箱后端的登记文件 {path}：{err}") from None
        backends = doc.get("backends")
        if not isinstance(backends, dict) or not backends:
            raise ValueError("沙箱后端的登记文件里没有 backends")
        environments = doc.get("environments")
        if environments is not None and not isinstance(environments, dict):
            raise ValueError("沙箱后端登记文件的 environments 要是一个对象")
        return cls(backends, str(doc.get("default") or ""), environments=environments, **kwargs)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    async def for_tenant(self, tenant_id: str) -> Backend:
        """这个租户用的后端：租户策略的 sandbox.backend，没写时 default。"""
        return await self.backend(await self.choice(tenant_id))

    async def choice(self, tenant_id: str) -> str:
        return (await self.policy_for(tenant_id)).backend

    async def policy_for(self, tenant_id: str) -> SandboxPolicy:
        """这个租户的沙箱策略（首选、回退、最低隔离强度、配额、空闲与活满），按租户缓存 cache_ttl 秒。
        首选或回退里有没登记的名字一律拒绝，不悄悄换后端。"""
        cached = self._policies.get(tenant_id)
        if cached and time.monotonic() - cached[0] < self._cache_ttl:
            return cached[1]
        doc = None
        if self._policy is not None:
            try:
                doc = await self._policy(tenant_id)
            except NotFoundError:
                doc = None
        policy = SandboxPolicy.from_document(doc, self.default)
        for name in policy.names:
            if name not in self._specs:
                raise InvalidError("unknown_sandbox_backend", f"租户策略要的沙箱后端 {name} 在这个部署里没有登记")
        if policy.min_isolation and policy.min_isolation not in ISOLATIONS:
            raise InvalidError("invalid_min_isolation", f"租户策略的 min_isolation 只能是 {'、'.join(ISOLATIONS)}")
        self._policies[tenant_id] = (time.monotonic(), policy)
        return policy

    def spec(self, name: str) -> Mapping[str, Any]:
        """登记里这个后端的原始配置（价目、工作区卷等，给 broker 用）。"""
        if name not in self._specs:
            raise InvalidError("unknown_sandbox_backend", f"沙箱后端 {name} 没有登记")
        return self._specs[name]

    def isolation(self, name: str) -> str:
        return str(self.spec(name).get("isolation", "container"))

    def image_for(self, name: str, runtime: str) -> str:
        """登记里这个后端给这个运行环境配的镜像（K8s 的 images，或只给了 image 时的 python）；没配时为空。"""
        spec = self.spec(name)
        images = dict(spec.get("images") or {})
        if runtime in images:
            return str(images[runtime])
        return str(spec.get("image") or "") if runtime == "python" else ""

    async def backend(self, name: str) -> Backend:
        if name not in self._specs:
            raise InvalidError("unknown_sandbox_backend", f"沙箱后端 {name} 没有登记")
        async with self._lock:
            backend = self._backends.get(name)
            if backend is None:
                spec = self._specs[name]
                provider = await self._build(name, spec)
                backend = Backend(name=name, kind=spec["kind"], isolation=spec.get("isolation", "container"), provider=provider)
                self._backends[name] = backend
            return backend

    async def read_secret(self, name: str) -> bytes | None:
        """SecretStore 平台作用域（sandbox）里的一个密钥；没有时 None。"""
        return await self._secret(name)

    async def read_tenant_secret(self, tenant_id: str, name: str) -> bytes | None:
        """租户自己的 SecretStore 里的一个密钥（测试用密钥 sandbox/test-env，D-16）；没有时 None。"""
        if self._secrets_file is None:
            return None
        from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
        from agentruntime_core.tenancy import TenantContext

        try:
            value = (await LocalFileSecretStoreProvider(self._secrets_file).bind(TenantContext(tenant_id=tenant_id)).read(name)).reveal()
        except NotFoundError:
            return None
        return value.encode() if isinstance(value, str) else value

    async def _secret(self, name: str) -> bytes | None:
        if self._secrets_file is None:
            return None
        from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider

        try:
            value = (await LocalFileSecretStoreProvider(self._secrets_file).bind_platform(SECRET_PURPOSE).read(name)).reveal()
        except NotFoundError:
            return None
        return value.encode() if isinstance(value, str) else value

    async def _build(self, name: str, spec: Mapping[str, Any]) -> SandboxProvider:
        kind = spec["kind"]
        if kind == "local":
            from agentruntime_core.dev.sandbox import LocalSubprocessSandboxProvider

            root = Path(tempfile.mkdtemp(prefix=f"ar-sandbox-{name}-"))
            return LocalSubprocessSandboxProvider(root, workspaces=self._workspace_root)
        if kind == "k8s":
            from agentruntime_core.adapters.k8s_sandbox import K8sPodSandboxProvider, K8sSandboxConfig
            from agentruntime_core.adapters.kube import KubeAPI

            fields = {k: spec[k] for k in ("namespace", "agent_image", "runtime_class", "connect", "workspace_claim", "image_pull_policy") if k in spec}
            if "agent_port" in spec:
                fields["agent_port"] = int(spec["agent_port"])
            if "run_as_user" in spec:
                fields["run_as_user"] = int(spec["run_as_user"])
            if "max_envs_per_scope" in spec:
                fields["max_envs_per_scope"] = int(spec["max_envs_per_scope"])
            if "startup_timeout_s" in spec:
                fields["startup_timeout"] = timedelta(seconds=float(spec["startup_timeout_s"]))
            if self.environments.get("registry"):
                # 按项目定制的运行环境（D-17）：接受本部署的环境镜像仓库里的镜像。
                reg = dict(self.environments["registry"])
                fields["custom_image_prefix"] = f"{reg['pull_host']}/{reg.get('prefix', 'envs')}".rstrip("/")
            # runtime → 镜像（D-14）：images 里列全；只给 image 时就是 python 的镜像（D-05 的写法）。
            images = {str(k): str(v) for k, v in dict(spec.get("images") or {}).items()}
            images.setdefault("python", str(spec.get("image") or "python:3.13-slim"))
            config = K8sSandboxConfig(images=images, **fields)
            return K8sPodSandboxProvider(KubeAPI.load(spec.get("kubeconfig") or None), config, token_key=await self._secret(TOKEN_KEY_SECRET))
        if kind == "e2b":
            from agentruntime_worker.tools import sandbox_e2b

            return await sandbox_e2b.build(name, spec, self._secret)
        from agentruntime_core.adapters.cloudflare_sandbox import CloudflareSandboxProvider

        token = await self._secret(BRIDGE_TOKEN_SECRET)
        if not token:
            raise InvalidError("missing_bridge_token", f"SecretStore 的平台作用域里没有 {BRIDGE_TOKEN_SECRET}（Cloudflare 后端 {name} 要用）")
        return CloudflareSandboxProvider(str(spec["bridge_url"]), token.decode())

    async def aclose(self) -> None:
        for backend in self._backends.values():
            close = getattr(backend.provider, "aclose", None)
            if close is not None:
                await close()
        self._backends.clear()


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


def parse_cpu_millis(value: str) -> int:
    """docker 写法的 CPU（"2"、"0.5"）换成毫核。"""
    return max(1, int(float(value) * 1000))


def parse_memory_mb(value: str) -> int:
    """docker 写法的内存（"2g"、"512m"）换成 MiB。"""
    v = value.strip().lower()
    unit = {"g": 1024, "m": 1, "k": 1 / 1024}.get(v[-1:], None)
    number = float(v[:-1]) if unit is not None else float(v) / (1024 * 1024)
    return max(16, int(number * (unit if unit is not None else 1)))


__all__ = ["Backend", "PolicyLookup", "Sandboxes", "parse_cpu_millis", "parse_memory_mb", "registry_policy"]
