"""按项目定制的运行环境的构建器（docs/plan/cloud-deploy.md D-17）：在 microVM 里构建不可信的 Dockerfile。

K8sJobBuilder 每次构建建一个一次性的 Job（命名空间默认 sandbox-builds）：

- RuntimeClass 必须是 microVM 级的（Kata；登记里 runtime_class，建 Builder 时核对 isolation = microvm）：BuildKit 要特权才能
  起容器，特权只在 VM 里面（Kata 配 privileged_without_host_devices，见 deploy/cluster），碰不到宿主。
- 不挂 ServiceAccount 令牌、不用宿主的网络、PID、IPC，不挂宿主路径；Dockerfile 由 ConfigMap 给，仓库里的文件（要上下文时）
  以只读的 subPath 挂本租户的这个工作区（与沙箱 Pod 同一块卷），别的租户的看不到。
- 联网只经包镜像与出网网关：构建的命名空间默认拒绝出网，网络策略只放行这两处；代理与镜像源的变量（带这次构建的短期凭据）
  放在这次构建自己的 Secret 里，Job 结束就删。
- 两步：initContainer 用 BuildKit（buildctl-daemonless.sh）构建，输出 OCI 打包到 emptyDir（sizeLimit 是镜像的大小上限，
  超了 kubelet 驱逐）；主容器跑 sandbox-agent，只读地挂着这个 emptyDir。Worker 等构建那一步正常退出、agent 就绪，按 Pod IP
  取回 image.tar（令牌是这次构建随机生成的，只在 Secret 与 Worker 的内存里），边取边核对大小上限，取完就删 Job。构建里
  没有镜像仓库的凭据，也不连 ObjectStore：推到仓库是 Worker 的事（sandbox.environments）。
- 超时：构建那一步超过 timeout 就删 Job（另有 activeDeadlineSeconds 兜底）；失败、超时、被驱逐都给出原因（失败时附上构建
  日志的最后几行，去掉地址与凭据）。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import IO, Any

from agentruntime_core.errors import InvalidError, NotFoundError, TransientError

from agentruntime_worker.sandbox.environments import BuildJob, BuildOutcome

MANAGED_BY = "agentruntime-env-builder"
# BuildKit 的版本写死（发布满两周）。
DEFAULT_BUILDKIT_IMAGE = "moby/buildkit:v0.25.1"
AGENT_PORT = 7070
ARTIFACT = "image.tar"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

# 取回构建产物：(地址, 令牌, 写到哪里, 最多多少字节) → 写了多少字节；超过上限 invalid（image_too_large）。
Fetch = Callable[[str, str, IO[bytes], int], Awaitable[int]]


@dataclass(frozen=True)
class BuilderConfig:
    agent_image: str
    namespace: str = "sandbox-builds"
    runtime_class: str = "kata"
    isolation: str = "microvm"
    buildkit_image: str = DEFAULT_BUILDKIT_IMAGE
    # 沙箱用的工作区卷（与 K8s 沙箱后端的 workspace_claim 相同；在构建的命名空间里也要有同名的 PVC 指向同一个卷）。
    workspace_claim: str = ""
    cpu_millis: int = 2000
    memory_mb: int = 4096
    image_pull_policy: str = "IfNotPresent"
    poll_interval: timedelta = timedelta(seconds=2)
    # 构建结束后留多久给 Worker 取回产物（activeDeadlineSeconds 在构建的超时之外再加这么多）。
    fetch_window: timedelta = timedelta(minutes=15)


def job_name(key: str) -> str:
    return f"envb-{key[:16]}-{secrets.token_hex(3)}"


def _owner_label(owner: str) -> str:
    return hashlib.sha256(owner.encode()).hexdigest()[:40]


BUILD_SCRIPT = r"""set -eu
args=""
for v in HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy PIP_INDEX_URL PIP_TRUSTED_HOST UV_DEFAULT_INDEX npm_config_registry GOPROXY; do
  eval "val=\${$v:-}"
  if [ -n "$val" ]; then args="$args --opt build-arg:$v=$val"; fi
done
# shellcheck disable=SC2086
buildctl-daemonless.sh build --frontend dockerfile.v0 \
  --local context=/context --local dockerfile=/dockerfile --opt filename=Dockerfile $args \
  --output type=oci,dest=/out/image.tar
"""


async def http_fetch(url: str, token: str, out: IO[bytes], limit: int) -> int:
    """从构建 Pod 里的 sandbox-agent 取回文件（不走代理；令牌放在 X-Sandbox-Token，不进日志与错误信息）。"""

    def run() -> int:
        req = urllib.request.Request(url, headers={"X-Sandbox-Token": token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=600) as resp:
                copied = 0
                while chunk := resp.read(1 << 20):
                    copied += len(chunk)
                    if copied > limit:
                        raise InvalidError("image_too_large", "构建产物超过大小上限")
                    out.write(chunk)
                return copied
        except urllib.error.HTTPError as err:
            raise TransientError("builder_fetch_failed", f"取不回构建产物（HTTP {err.code}）") from None
        except (urllib.error.URLError, OSError) as err:
            raise TransientError("builder_fetch_failed", f"取不回构建产物（{type(err).__name__}）") from None

    return await asyncio.to_thread(run)


class K8sJobBuilder:
    def __init__(self, kube: Any, config: BuilderConfig, *, fetch: Fetch = http_fetch) -> None:
        if config.isolation != "microvm" or not config.runtime_class:
            raise ValueError("构建不可信的 Dockerfile 只能在 microVM 里：runtime_class 要是 Kata 一类、isolation 要是 microvm")
        if not _NAME.match(config.namespace):
            raise ValueError("构建的命名空间格式不对")
        self.kube = kube
        self.config = config
        self.fetch = fetch

    # 清单

    def manifests(self, job: BuildJob, name: str, token: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """这次构建的 ConfigMap（Dockerfile）、Secret（取回产物的令牌与联网变量）与 Job。"""
        c = self.config
        labels = {"app.kubernetes.io/managed-by": MANAGED_BY, "agentruntime.io/owner": _owner_label(job.owner), "agentruntime.io/network": "build"}
        meta = {"name": name, "namespace": c.namespace, "labels": labels, "annotations": {"agentruntime.io/env-key": job.key}}
        config_map = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta, "data": {"Dockerfile": job.dockerfile}}
        net_env = {k: v for k, v in job.env.items() if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) and not k.startswith("AGENT_")}
        secret = {"apiVersion": "v1", "kind": "Secret", "metadata": meta, "type": "Opaque", "stringData": {"AGENT_TOKEN": token, **net_env}}
        # 构建只拿联网的变量；令牌只给提供产物的那一步。
        build_env = [{"name": k, "valueFrom": {"secretKeyRef": {"name": name, "key": k}}} for k in sorted(net_env)]
        unprivileged = {
            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
        }
        context_volume: dict[str, Any] = {"name": "context", "emptyDir": {"sizeLimit": "16Mi"}}
        context_mount: dict[str, Any] = {"name": "context", "mountPath": "/context", "readOnly": True}
        if job.context:
            if not c.workspace_claim:
                raise InvalidError("unsupported_workspace", "构建要用仓库里的文件，但构建器没有配置工作区卷")
            if not _NAME.match(job.owner) or not _NAME.match(job.workspace_id or "default"):
                raise InvalidError("invalid_workspace", "租户或 workspace 的格式不对")
            sub = f"{job.owner}/{job.workspace_id or 'default'}"
            if job.context not in ("", "."):
                if job.context.startswith("/") or ".." in job.context.split("/"):
                    raise InvalidError("invalid_context", "构建的上下文出了仓库")
                sub += "/" + job.context
            context_volume = {"name": "context", "persistentVolumeClaim": {"claimName": c.workspace_claim, "readOnly": True}}
            context_mount = {"name": "context", "mountPath": "/context", "readOnly": True, "subPath": sub}
        deadline = int((job.timeout + c.fetch_window).total_seconds())
        pod_spec = {
            "runtimeClassName": c.runtime_class,
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "hostNetwork": False,
            "hostPID": False,
            "hostIPC": False,
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 0,
            "initContainers": [{
                "name": "build",
                "image": c.buildkit_image,
                "imagePullPolicy": c.image_pull_policy,
                "command": ["sh", "-c", BUILD_SCRIPT],
                "env": build_env,
                # 特权只在 Kata 的 VM 里（privileged_without_host_devices），碰不到宿主的设备。
                "securityContext": {"privileged": True},
                "resources": {
                    "requests": {"cpu": f"{c.cpu_millis}m", "memory": f"{c.memory_mb}Mi"},
                    "limits": {"cpu": f"{c.cpu_millis}m", "memory": f"{c.memory_mb}Mi", "ephemeral-storage": f"{job.max_bytes * 3 // (1 << 20)}Mi"},
                },
                "volumeMounts": [
                    context_mount,
                    {"name": "dockerfile", "mountPath": "/dockerfile", "readOnly": True},
                    {"name": "out", "mountPath": "/out"},
                ],
            }],
            "containers": [{
                # 只读地提供产物：Worker 带着令牌来取（GET /v1/files/image.tar）。
                "name": "serve",
                "image": c.agent_image,
                "imagePullPolicy": c.image_pull_policy,
                "command": ["/sandbox-agent"],
                "env": [
                    {"name": "AGENT_TOKEN", "valueFrom": {"secretKeyRef": {"name": name, "key": "AGENT_TOKEN"}}},
                    {"name": "AGENT_WORKDIR", "value": "/out"},
                    {"name": "AGENT_ADDR", "value": f":{AGENT_PORT}"},
                    {"name": "AGENT_MAX_TIMEOUT_S", "value": "30"},
                ],
                "ports": [{"name": "agent", "containerPort": AGENT_PORT}],
                "readinessProbe": {"httpGet": {"path": "/healthz", "port": AGENT_PORT}, "periodSeconds": 2},
                "securityContext": unprivileged,
                "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"cpu": "1", "memory": "256Mi"}},
                "volumeMounts": [{"name": "out", "mountPath": "/out", "readOnly": True}],
            }],
            "volumes": [
                context_volume,
                {"name": "dockerfile", "configMap": {"name": name}},
                {"name": "out", "emptyDir": {"sizeLimit": f"{job.max_bytes // (1 << 20)}Mi"}},
            ],
        }
        job_manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": meta,
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": deadline,
                "ttlSecondsAfterFinished": 600,
                "template": {"metadata": {"labels": labels}, "spec": pod_spec},
            },
        }
        return config_map, secret, job_manifest

    # 运行

    def _path(self, kind: str, name: str = "") -> str:
        group = "/apis/batch/v1" if kind == "jobs" else "/api/v1"
        base = f"{group}/namespaces/{self.config.namespace}/{kind}"
        return f"{base}/{name}" if name else base

    async def build(self, job: BuildJob, out: IO[bytes]) -> BuildOutcome:
        name = job_name(job.key)
        token = secrets.token_urlsafe(32)
        config_map, secret, manifest = self.manifests(job, name, token)
        await self.kube.json("POST", self._path("configmaps"), config_map)
        await self.kube.json("POST", self._path("secrets"), secret)
        try:
            await self.kube.json("POST", self._path("jobs"), manifest)
            return await self._wait(name, job, token, out)
        finally:
            for kind in ("jobs", "secrets", "configmaps"):
                body = {"propagationPolicy": "Background"} if kind == "jobs" else None
                try:
                    await self.kube.request("DELETE", self._path(kind, name), body)
                except TransientError:
                    pass

    async def _pod(self, name: str) -> dict[str, Any] | None:
        pods = await self.kube.json("GET", self._path("pods") + f"?labelSelector=job-name%3D{name}")
        items = pods.get("items") or []
        return items[0] if items else None

    async def _wait(self, name: str, job: BuildJob, token: str, out: IO[bytes]) -> BuildOutcome:
        timed_out = BuildOutcome(ok=False, reason=f"构建超时（{int(job.timeout.total_seconds())} 秒）", failure="timed_out")
        build_deadline = time.monotonic() + job.timeout.total_seconds()
        serve_deadline = build_deadline + self.config.fetch_window.total_seconds()
        while True:
            try:
                status = (await self.kube.json("GET", self._path("jobs", name))).get("status") or {}
            except NotFoundError:
                return BuildOutcome(ok=False, reason="构建的 Job 不见了", failure="failed")
            for cond in status.get("conditions") or []:
                if cond.get("type") == "Failed" and cond.get("status") == "True":
                    if cond.get("reason") == "DeadlineExceeded":
                        return timed_out
                    return await self._failure(name, job, str(cond.get("reason") or ""))
            pod = await self._pod(name)
            pod_status = (pod or {}).get("status") or {}
            if pod_status.get("reason") == "Evicted":
                return BuildOutcome(ok=False, reason="镜像超过大小上限，构建被终止", failure="too_large")
            build = next((s for s in pod_status.get("initContainerStatuses") or [] if s.get("name") == "build"), {})
            terminated = (build.get("state") or {}).get("terminated") or {}
            if terminated:
                if terminated.get("exitCode") != 0:
                    return await self._failure(name, job, str(terminated.get("reason") or ""))
                serve = next((s for s in pod_status.get("containerStatuses") or [] if s.get("name") == "serve"), {})
                if serve.get("ready") and pod_status.get("podIP"):
                    return await self._collect(pod_status["podIP"], token, out, job)
                if time.monotonic() > serve_deadline:
                    return BuildOutcome(ok=False, reason="构建完成了，但取不回产物（提供产物的容器没有就绪）", failure="failed")
            elif time.monotonic() > build_deadline:
                return timed_out
            await asyncio.sleep(self.config.poll_interval.total_seconds())

    async def _collect(self, pod_ip: str, token: str, out: IO[bytes], job: BuildJob) -> BuildOutcome:
        host = f"[{pod_ip}]" if ":" in pod_ip else pod_ip
        try:
            await self.fetch(f"http://{host}:{AGENT_PORT}/v1/files/{ARTIFACT}", token, out, job.max_bytes)
        except InvalidError as err:
            if err.code == "image_too_large":
                return BuildOutcome(ok=False, reason="镜像超过大小上限，构建被终止", failure="too_large")
            raise
        except TransientError as err:
            return BuildOutcome(ok=False, reason=err.message, failure="failed")
        return BuildOutcome(ok=True)

    async def _failure(self, name: str, job: BuildJob, reason: str) -> BuildOutcome:
        tail = await self._logs(name, job)
        if tail in ("Evicted",) or "ephemeral" in tail.lower():
            return BuildOutcome(ok=False, reason="镜像超过大小上限，构建被终止", failure="too_large")
        if tail == "OOMKilled":
            return BuildOutcome(ok=False, reason="构建用光了内存（OOMKilled）", failure="failed")
        return BuildOutcome(ok=False, reason=("构建失败：" + tail) if tail else f"构建失败（{reason or 'Failed'}）", failure="failed")

    async def _logs(self, name: str, job: BuildJob) -> str:
        """构建日志的最后几行（去掉地址与凭据），给用户看失败原因。"""
        try:
            pod = await self._pod(name)
        except (NotFoundError, TransientError):
            return ""
        if pod is None:
            return ""
        status = pod.get("status") or {}
        if status.get("reason") == "Evicted":
            return "Evicted"
        for cs in [*(status.get("initContainerStatuses") or []), *(status.get("containerStatuses") or [])]:
            term = (cs.get("state") or {}).get("terminated") or {}
            if term.get("reason") in ("Evicted", "OOMKilled"):
                return str(term.get("reason"))
        pod_name = (pod.get("metadata") or {}).get("name", "")
        code, data = await self.kube.request("GET", self._path("pods", pod_name) + "/log?container=build&tailLines=20")
        if code == 200:
            return redact(data.decode("utf-8", errors="replace"), job)[-1500:]
        return ""


def redact(text: str, job: BuildJob) -> str:
    """日志里去掉联网变量的值（里面有凭据）与环境凭据的样子。"""
    for value in job.env.values():
        if value and len(value) >= 8:
            text = text.replace(value, "***")
    return re.sub(r"ar1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "***", text)


def builder_from_spec(spec: Mapping[str, Any], kube: Any) -> K8sJobBuilder:
    """后端登记里 environments.builder 一节（kind k8s）建 Builder。"""
    fields: dict[str, Any] = {k: spec[k] for k in ("namespace", "runtime_class", "isolation", "buildkit_image", "workspace_claim", "image_pull_policy") if k in spec}
    for k in ("cpu_millis", "memory_mb"):
        if k in spec:
            fields[k] = int(spec[k])
    return K8sJobBuilder(kube, BuilderConfig(agent_image=str(spec["agent_image"]), **fields))


__all__ = ["AGENT_PORT", "BUILD_SCRIPT", "BuilderConfig", "Fetch", "K8sJobBuilder", "MANAGED_BY", "builder_from_spec", "http_fetch", "job_name", "redact"]
