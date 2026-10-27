"""Sandbox 的 K8s 实现：每个环境一个一次性 Pod（设计书 4.10、附录 L.11；计划 docs/plan/cloud-deploy.md D-05）。语义见 agentruntime_core.providers.sandbox。

Pod 的样子：
- RuntimeClass 由配置给出（gvisor 是容器级隔离，kata 是 microVM 级）；空串用集群默认，只给开发与测试。
- initContainer 用 agent 镜像把 sandbox-agent 拷进一个 emptyDir，跑命令的容器（配置里按 runtime 选的镜像，例如 python:3.13-slim）
  以它为入口；命令、读写文件都经 agent（adapters.sandbox_agent）。镜像不用自带 agent。
- 非 root（run_as_user）、根文件系统只读、去掉全部 capability、不许提权、seccomp RuntimeDefault、不挂 ServiceAccount 令牌、
  不注入 Service 的环境变量；/tmp 与工作目录是 emptyDir。资源请求与上限取自 SandboxSpec。
- 网络：只支持断网（标签 agentruntime.io/network=none，命名空间里默认拒绝出网的 NetworkPolicy 起作用，见 deploy/cluster）。
  按域名放行做不到（NetworkPolicy 只认 IP），allowlist 报 invalid：联网的要用 microVM 后端（Cloudflare，D-07）。
- workspace_id 非空时，工作目录挂本租户的这个 workspace：沙箱命名空间里的 PVC（workspace_claim）的 subPath <tenant>/<workspace>。
  这个 PVC 与 Worker 命名空间里的 PVC 绑着指向同一个节点目录的两个 PV（deploy/cluster/workspaces.yaml）：Pod Security 的
  restricted 级别不许 Pod 直接用 hostPath，PVC 又不能跨命名空间共用。单节点部署这样做；工作区跟着沙箱走要等 D-06。

作用域：Pod 的注解里记着所属作用域（租户 ID 或 _platform），标签里是它的哈希；别的作用域访问一律 not_found。
agent 的令牌由 token_key 对 env_id 做 HMAC 得到，不存放：同一套配置的任何 Worker 进程都能连上，别人猜不出。
空闲超时：注解里记着到期时间，每次用时顺延；新建环境时顺手删掉到期的 Pod（最多每分钟一次）。activeDeadlineSeconds 兜底。
每个作用域的配额（max_envs_per_scope，docs/plan/cloud-deploy.md D-10）：新建前按标签数一数这个作用域还活着的 Pod，到上限就 denied
（sandbox_quota_exceeded）。数的是集群里的 Pod，所以对所有 Worker 副本一起生效；同时并发新建时可能多出几个，是软上限。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from agentruntime_core.adapters.kube import KubeAPI, error_for, label_selector
from agentruntime_core.adapters.sandbox_agent import AgentClient, AgentEndpoint, check_path
from agentruntime_core.dev._common import scope_id
from agentruntime_core.dev.sandbox import workspace_subpath
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError, TransientError
from agentruntime_core.providers.sandbox import ExecResult, ListedEnvironment, SandboxEnv, SandboxSpec
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector

MANAGED_BY = "agentruntime-sandbox"
LABELS = {"app.kubernetes.io/managed-by": MANAGED_BY}
OWNER_LABEL = "agentruntime.io/owner"
NETWORK_LABEL = "agentruntime.io/network"
OWNER_ANNOTATION = "agentruntime.io/owner"
EXPIRES_ANNOTATION = "agentruntime.io/expires-at"
_ENV_ID = re.compile(r"^sbx-[0-9a-f]{16}$")
# 拉镜像失败、配置不对、一直起不来：不再等，删掉 Pod 报 transient。
_FATAL_WAITING = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CreateContainerConfigError", "CreateContainerError", "CrashLoopBackOff"}


@dataclass(frozen=True)
class K8sSandboxConfig:
    namespace: str = "sandboxes"
    # runtime → 跑命令的镜像。
    images: Mapping[str, str] = field(default_factory=lambda: {"python": "python:3.13-slim"})
    agent_image: str = "ghcr.io/agentruntime/sandbox-agent:0.1.0"
    # gvisor、kata 等 RuntimeClass；空串用集群默认（只给开发与测试）。
    runtime_class: str = ""
    # pod_ip：集群内直连 Pod；api_proxy：经 API Server 的 pods/proxy（集群外的开发与测试）。
    connect: str = "pod_ip"
    agent_port: int = 7070
    # 绑定 workspace 时挂的 PVC（沙箱命名空间里的，用 subPath）；为空时不支持 workspace。
    workspace_claim: str = ""
    run_as_user: int = 10001
    image_pull_policy: str = "IfNotPresent"
    tmp_size_mb: int = 512
    startup_timeout: timedelta = timedelta(minutes=2)
    max_lifetime: timedelta = timedelta(hours=6)
    # 一个作用域（租户）同时最多几个环境；0 表示不限（只受命名空间的 ResourceQuota 约束）。
    max_envs_per_scope: int = 0


class K8sPodSandboxProvider:
    def __init__(
        self,
        kube: KubeAPI,
        config: K8sSandboxConfig,
        *,
        token_key: bytes | None = None,
        faults: FaultInjector | None = None,
        agent: AgentClient | None = None,
    ) -> None:
        if config.connect not in ("pod_ip", "api_proxy"):
            raise ValueError("connect 只能是 pod_ip 或 api_proxy")
        self.kube = kube
        self.config = config
        # 没给 token_key 时用进程内随机的：只有这个进程能连上自己建的环境（单进程开发够用）。
        self._token_key = token_key or secrets.token_bytes(32)
        self._faults = faults or FaultInjector()
        self._agent = agent or AgentClient()
        self._destroyed: dict[str, str] = {}
        self._last_reap = 0.0
        self._last_touch: dict[str, float] = {}

    def bind(self, tenant: TenantContext) -> K8sPodSandbox:
        return K8sPodSandbox(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> K8sPodSandbox:
        return K8sPodSandbox(self, platform_scope(purpose))

    async def aclose(self) -> None:
        self.kube.close()

    # 给 Pod 与连接用的小工具

    def token(self, env_id: str) -> str:
        return hmac.new(self._token_key, env_id.encode(), hashlib.sha256).hexdigest()

    def pods_path(self, name: str = "") -> str:
        base = f"/api/v1/namespaces/{self.config.namespace}/pods"
        return f"{base}/{name}" if name else base

    def endpoint(self, env_id: str, pod: Mapping[str, Any]) -> AgentEndpoint:
        token = self.token(env_id)
        if self.config.connect == "api_proxy":
            url = self.kube.pod_proxy_url(self.config.namespace, env_id, self.config.agent_port)
            return AgentEndpoint(url, token, headers=self.kube.auth_headers(), ssl_context=self.kube.ssl_context)
        ip = (pod.get("status") or {}).get("podIP")
        if not ip:
            raise TransientError("sandbox_unavailable", "沙箱 Pod 还没有地址")
        host = f"[{ip}]" if ":" in ip else ip
        return AgentEndpoint(f"http://{host}:{self.config.agent_port}", token)

    async def reap(self, *, force: bool = False) -> int:
        """删掉空闲到期的沙箱 Pod，返回删了几个。新建环境时顺手调（最多每分钟一次）。"""
        now = time.monotonic()
        if not force and now - self._last_reap < 60:
            return 0
        self._last_reap = now
        listed = await self.kube.json("GET", self.pods_path() + "?labelSelector=" + label_selector(LABELS))
        deleted = 0
        for pod in listed.get("items") or []:
            meta = pod.get("metadata") or {}
            expires = (meta.get("annotations") or {}).get(EXPIRES_ANNOTATION, "")
            if expires and _parse_time(expires) < datetime.now(UTC) and not meta.get("deletionTimestamp"):
                await self._delete(meta.get("name", ""))
                deleted += 1
        return deleted

    async def list_environments(self) -> list[ListedEnvironment]:
        """本部署建的沙箱 Pod（按 managed-by 标签；正在删除的不算），给沙箱环境表对账（D-11）。"""
        listed = await self.kube.json("GET", self.pods_path() + "?labelSelector=" + label_selector(LABELS))
        out: list[ListedEnvironment] = []
        for pod in listed.get("items") or []:
            meta = pod.get("metadata") or {}
            name = str(meta.get("name") or "")
            if not _ENV_ID.match(name) or meta.get("deletionTimestamp"):
                continue
            owner = str((meta.get("annotations") or {}).get(OWNER_ANNOTATION) or "")
            created = meta.get("creationTimestamp")
            phase = str((pod.get("status") or {}).get("phase") or "")
            out.append(ListedEnvironment(name, owner, _parse_time(created) if created else None, "running" if phase in ("Pending", "Running") else phase.lower()))
        return out

    async def _delete(self, name: str) -> None:
        status, _ = await self.kube.request("DELETE", self.pods_path(name), {"gracePeriodSeconds": 0, "propagationPolicy": "Background"})
        if status not in (200, 202, 404):
            raise TransientError("sandbox_unavailable", f"删不掉沙箱 Pod（HTTP {status}）")


class K8sPodSandbox:
    def __init__(self, provider: K8sPodSandboxProvider, scope: Scope) -> None:
        self._p = provider
        self._scope = scope
        self._owner = scope_id(scope)
        self._owner_label = hashlib.sha256(self._owner.encode()).hexdigest()[:40]

    async def create(self, spec: SandboxSpec) -> SandboxEnv:
        p, c = self._p, self._p.config
        p._faults.check("create")
        image = c.images.get(spec.runtime)
        if not image:
            raise InvalidError("unsupported_runtime", f"没有配置 runtime {spec.runtime} 的镜像")
        if spec.network != "none":
            raise InvalidError("network_allowlist_unsupported", "K8s 沙箱只支持断网；按域名放行要用 microVM 后端")
        if spec.cpu_millis <= 0 or spec.memory_mb <= 0 or spec.idle_timeout <= timedelta(0):
            raise InvalidError("invalid_argument", "cpu_millis、memory_mb、idle_timeout 都要大于 0")
        workspace = self._workspace_volume(spec.workspace_id) if spec.workspace_id else None
        await p.reap()
        if c.max_envs_per_scope > 0:
            selector = label_selector({**LABELS, OWNER_LABEL: self._owner_label})
            listed = await p.kube.json("GET", p.pods_path() + "?labelSelector=" + selector)
            alive = [pod for pod in listed.get("items") or [] if not (pod.get("metadata") or {}).get("deletionTimestamp")]
            if len(alive) >= c.max_envs_per_scope:
                raise DeniedError("sandbox_quota_exceeded", f"同时最多 {c.max_envs_per_scope} 个沙箱，先等前面的跑完")
        env_id = "sbx-" + secrets.token_hex(8)
        manifest = self._manifest(env_id, spec, image, workspace)
        await p.kube.json("POST", p.pods_path(), manifest)
        try:
            pod = await self._wait_ready(env_id)
            endpoint = p.endpoint(env_id, pod)
            deadline = time.monotonic() + 30
            while not await p._agent.healthy(endpoint):
                if time.monotonic() > deadline:
                    raise TransientError("sandbox_unavailable", "沙箱里的 agent 没有就绪")
                await asyncio.sleep(0.3)
        except BaseException:
            await p._delete(env_id)
            raise
        p._last_touch[env_id] = time.monotonic()
        return SandboxEnv(env_id=env_id, spec=spec, image=_image_id(pod))

    async def exec(
        self, env_id: str, argv: Sequence[str], *, timeout: timedelta, stdin: bytes = b"", env: Mapping[str, str] | None = None
    ) -> ExecResult:
        self._p._faults.check("exec")
        pod = await self._lookup(env_id)
        if not argv:
            raise InvalidError("invalid_argument", "argv 不能为空")
        await self._touch(env_id, pod)
        return await self._p._agent.exec(self._p.endpoint(env_id, pod), argv, timeout=timeout, stdin=stdin, env=env)

    async def write_file(self, env_id: str, path: str, data: bytes) -> None:
        self._p._faults.check("write_file")
        check_path(path)
        pod = await self._lookup(env_id)
        await self._p._agent.write_file(self._p.endpoint(env_id, pod), path, data)

    async def read_file(self, env_id: str, path: str) -> bytes:
        self._p._faults.check("read_file")
        check_path(path)
        pod = await self._lookup(env_id)
        return await self._p._agent.read_file(self._p.endpoint(env_id, pod), path)

    async def preview_address(self, env_id: str, port: int) -> str:
        """预览地址转发到 Pod 的这个端口（出网网关在集群里直连 Pod；沙箱命名空间的网络策略只放行网关进来，D-16）。"""
        if not 1 <= int(port) <= 65535 or int(port) == self._p.config.agent_port:
            raise InvalidError("invalid_port", "端口要在 1 到 65535 之间，且不能是 agent 的端口")
        pod = await self._lookup(env_id)
        ip = (pod.get("status") or {}).get("podIP")
        if not ip:
            raise TransientError("sandbox_unavailable", "沙箱 Pod 还没有地址")
        return f"[{ip}]:{int(port)}" if ":" in ip else f"{ip}:{int(port)}"

    async def destroy(self, env_id: str) -> None:
        p = self._p
        p._faults.check("destroy")
        if not isinstance(env_id, str) or not _ENV_ID.match(env_id):
            raise NotFoundError("unknown_env", "环境不存在")
        if p._destroyed.get(env_id) == self._owner:
            return
        status, data = await p.kube.request("GET", p.pods_path(env_id))
        if status == 404:
            return  # 已经没了：重复删除不报错，也没有东西可泄露
        if status != 200:
            raise error_for(status, data)
        pod = json.loads(data)
        if not self._mine(pod):
            raise NotFoundError("unknown_env", "环境不存在")
        await p._delete(env_id)
        p._destroyed[env_id] = self._owner
        p._last_touch.pop(env_id, None)

    # 内部

    def _mine(self, pod: Mapping[str, Any]) -> bool:
        meta = pod.get("metadata") or {}
        labels, annotations = meta.get("labels") or {}, meta.get("annotations") or {}
        return labels.get("app.kubernetes.io/managed-by") == MANAGED_BY and annotations.get(OWNER_ANNOTATION) == self._owner

    async def _lookup(self, env_id: str) -> dict[str, Any]:
        p = self._p
        if not isinstance(env_id, str) or not _ENV_ID.match(env_id) or env_id in p._destroyed:
            raise NotFoundError("unknown_env", "环境不存在")
        try:
            pod = await p.kube.json("GET", p.pods_path(env_id))
        except NotFoundError:
            raise NotFoundError("unknown_env", "环境不存在") from None
        if not self._mine(pod) or (pod.get("metadata") or {}).get("deletionTimestamp"):
            raise NotFoundError("unknown_env", "环境不存在")
        return pod

    async def _touch(self, env_id: str, pod: Mapping[str, Any]) -> None:
        """顺延空闲到期时间；一分钟内用过就不再写。"""
        p = self._p
        now = time.monotonic()
        if now - p._last_touch.get(env_id, 0.0) < 60:
            return
        idle = int((pod.get("metadata") or {}).get("annotations", {}).get("agentruntime.io/idle-seconds", "600"))
        patch = {"metadata": {"annotations": {EXPIRES_ANNOTATION: _format_time(datetime.now(UTC) + timedelta(seconds=idle))}}}
        await p.kube.json("PATCH", p.pods_path(env_id), patch, content_type="application/merge-patch+json")
        p._last_touch[env_id] = now

    async def _wait_ready(self, env_id: str) -> dict[str, Any]:
        p = self._p
        deadline = time.monotonic() + p.config.startup_timeout.total_seconds()
        while True:
            pod = await p.kube.json("GET", p.pods_path(env_id))
            status = pod.get("status") or {}
            phase = status.get("phase")
            if phase in ("Failed", "Succeeded"):
                raise TransientError("sandbox_unavailable", f"沙箱 Pod 意外结束（{phase}）")
            for cs in [*(status.get("initContainerStatuses") or []), *(status.get("containerStatuses") or [])]:
                reason = ((cs.get("state") or {}).get("waiting") or {}).get("reason")
                if reason in _FATAL_WAITING:
                    raise TransientError("sandbox_unavailable", f"沙箱 Pod 起不来（{reason}）")
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])
            if phase == "Running" and ready:
                return pod
            if time.monotonic() > deadline:
                raise TransientError("sandbox_unavailable", "沙箱 Pod 在时限内没有就绪")
            await asyncio.sleep(0.5)

    def _workspace_volume(self, workspace_id: str) -> tuple[dict[str, Any], str]:
        c = self._p.config
        if not c.workspace_claim:
            raise InvalidError("unsupported_workspace", "这个 Sandbox 没有配置工作区，不能绑定 workspace")
        # 与开发实现同一套校验：租户作用域、workspace_id 的格式。Worker 事先建好目录（属主与沙箱的用户相同；
        # subPath 不存在时 kubelet 会以 root 建，沙箱的用户就写不进去）。
        sub = workspace_subpath(self._scope, workspace_id)
        return {"name": "workdir", "persistentVolumeClaim": {"claimName": c.workspace_claim}}, sub

    def _manifest(self, env_id: str, spec: SandboxSpec, image: str, workspace: tuple[dict[str, Any], str] | None) -> dict[str, Any]:
        c = self._p.config
        idle = int(spec.idle_timeout.total_seconds())
        security = {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
            "runAsUser": c.run_as_user,
            "runAsGroup": c.run_as_user,
        }
        workdir_volume, sub_path = workspace if workspace else ({"name": "workdir", "emptyDir": {}}, "")
        workdir_mount: dict[str, Any] = {"name": "workdir", "mountPath": "/workspace"}
        if sub_path:
            workdir_mount["subPath"] = sub_path
        resources = {
            "requests": {"cpu": f"{spec.cpu_millis}m", "memory": f"{spec.memory_mb}Mi"},
            "limits": {"cpu": f"{spec.cpu_millis}m", "memory": f"{spec.memory_mb}Mi", "ephemeral-storage": "2Gi"},
        }
        pod_spec: dict[str, Any] = {
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "restartPolicy": "Never",
            "activeDeadlineSeconds": int(c.max_lifetime.total_seconds()),
            "terminationGracePeriodSeconds": 0,
            "securityContext": {"runAsNonRoot": True, "runAsUser": c.run_as_user, "runAsGroup": c.run_as_user, "fsGroup": c.run_as_user, "seccompProfile": {"type": "RuntimeDefault"}},
            "initContainers": [{
                "name": "install-agent",
                "image": c.agent_image,
                "imagePullPolicy": c.image_pull_policy,
                "command": ["/sandbox-agent", "install", "/agent/sandbox-agent"],
                "securityContext": security,
                "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}, "limits": {"cpu": "200m", "memory": "64Mi"}},
                "volumeMounts": [{"name": "agent", "mountPath": "/agent"}],
            }],
            "containers": [{
                "name": "sandbox",
                "image": image,
                "imagePullPolicy": c.image_pull_policy,
                "command": ["/agent/sandbox-agent"],
                "workingDir": "/workspace",
                "env": [
                    {"name": "AGENT_TOKEN", "value": self._p.token(env_id)},
                    {"name": "AGENT_WORKDIR", "value": "/workspace"},
                    {"name": "AGENT_ADDR", "value": f":{c.agent_port}"},
                    {"name": "AGENT_MAX_TIMEOUT_S", "value": str(int(c.max_lifetime.total_seconds()))},
                    {"name": "HOME", "value": "/tmp"},
                ],
                "ports": [{"name": "agent", "containerPort": c.agent_port}],
                "readinessProbe": {"httpGet": {"path": "/healthz", "port": c.agent_port}, "periodSeconds": 1, "failureThreshold": 60},
                "securityContext": security,
                "resources": resources,
                "volumeMounts": [
                    {"name": "agent", "mountPath": "/agent", "readOnly": True},
                    {"name": "tmp", "mountPath": "/tmp"},
                    workdir_mount,
                ],
            }],
            "volumes": [
                {"name": "agent", "emptyDir": {"sizeLimit": "64Mi"}},
                {"name": "tmp", "emptyDir": {"sizeLimit": f"{c.tmp_size_mb}Mi"}},
                workdir_volume,
            ],
        }
        if c.runtime_class:
            pod_spec["runtimeClassName"] = c.runtime_class
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": env_id,
                "namespace": c.namespace,
                "labels": {**LABELS, OWNER_LABEL: self._owner_label, NETWORK_LABEL: "none"},
                "annotations": {
                    OWNER_ANNOTATION: self._owner,
                    "agentruntime.io/idle-seconds": str(idle),
                    EXPIRES_ANNOTATION: _format_time(datetime.now(UTC) + spec.idle_timeout),
                },
            },
            "spec": pod_spec,
        }


def _image_id(pod: Mapping[str, Any]) -> str:
    """跑命令的容器实际用的镜像（kubelet 报的 imageID，按 digest）；还没报时为空。"""
    for cs in (pod.get("status") or {}).get("containerStatuses") or []:
        if cs.get("name") == "sandbox":
            image = str(cs.get("imageID") or "")
            return image.removeprefix("docker-pullable://")
    return ""


def _format_time(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return datetime.max.replace(tzinfo=UTC)
