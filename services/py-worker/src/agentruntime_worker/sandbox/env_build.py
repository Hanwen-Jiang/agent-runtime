"""构建工作流的 activity 与起工作流的 starter（D-17；工作流在 workflows/env_build.py）。"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from temporalio import activity
from temporalio.common import WorkflowIDConflictPolicy

from agentruntime_worker import names
from agentruntime_worker.sandbox.environments import BuildStarter, EnvironmentManager, Limits, RegistryPath
from agentruntime_worker.workflows.env_build import EnvBuildInput, build_workflow_id

if TYPE_CHECKING:
    from agentruntime_worker.sandbox.broker import SandboxBroker

log = logging.getLogger(__name__)


class EnvBuildActivities:
    def __init__(self, broker: SandboxBroker | None) -> None:
        self.broker = broker

    @activity.defn(name=names.BUILD_ENVIRONMENT)
    async def build_environment(self, inp: EnvBuildInput) -> str:
        manager = self.broker.environments if self.broker is not None else None
        if manager is None:
            return "unsupported"
        record = await manager.run_build(inp.tenant_id, inp.key)
        log.info("项目环境 %s（租户 %s）构建结束：%s %s", inp.key[:12], inp.tenant_id, record.state, record.reason)
        return record.state

    def activities(self) -> dict[str, object]:
        return {names.BUILD_ENVIRONMENT: self.build_environment}


REGISTRY_SECRET = "sandbox/env-registry"


class LazyRegistry:
    """镜像仓库的客户端，第一次推或删时才读凭据（SecretStore 平台作用域的 sandbox/env-registry：{"username","password"}）。"""

    def __init__(self, url: str, pull_host: str, read_secret: Callable[[str], Awaitable[bytes | None]]) -> None:
        self.url = url
        self.host = pull_host
        self._read_secret = read_secret
        self._client: Any = None

    async def _get(self) -> Any:
        if self._client is None:
            from agentruntime_core.adapters.oci_registry import RegistryClient

            raw = await self._read_secret(REGISTRY_SECRET)
            creds = json.loads(raw) if raw else {}
            self._client = RegistryClient(self.url, username=str(creds.get("username") or ""), password=str(creds.get("password") or ""), pull_host=self.host)
        return self._client

    def reference(self, repo: str, digest: str) -> str:
        return f"{self.host}/{repo}@{digest}"

    async def push(self, repo: str, tag: str, layout: Any) -> str:
        return await (await self._get()).push(repo, tag, layout)

    async def delete(self, repo: str, digest: str) -> bool:
        return await (await self._get()).delete(repo, digest)


def environment_manager(broker: SandboxBroker, spec: Mapping[str, Any]) -> EnvironmentManager:
    """按后端登记文件的 environments 一节建 EnvironmentManager（D-17）：

        "environments": {
          "base_image": "ghcr.io/agentruntime/sandbox-universal:0.1.0", "base_digest": "sha256:…",
          "registry": {"url": "http://…-env-registry.agent-runtime.svc:5000", "pull_host": "127.0.0.1:30500", "prefix": "envs"},
          "builder": {"kind": "k8s", "namespace": "sandbox-builds", "runtime_class": "kata", "isolation": "microvm",
                      "agent_image": "ghcr.io/agentruntime/sandbox-agent:0.1.0", "workspace_claim": "workspaces"},
          "build_hosts": ["registry-1.docker.io", "auth.docker.io", "production.cloudflare.docker.com", "deb.debian.org"],
          "limits": {"max_images": 5, "max_image_bytes": 8589934592, "build_timeout_s": 1800, "idle_days": 14}
        }
    """
    from agentruntime_worker.sandbox.broker import BuildNetwork
    from agentruntime_worker.sandbox.env_builder import builder_from_spec

    assert broker.ledger is not None
    registry_spec = dict(spec["registry"])
    registry = RegistryPath(host=str(registry_spec["pull_host"]), prefix=str(registry_spec.get("prefix", "envs")).strip("/"))
    limits_spec = dict(spec.get("limits") or {})
    limits = Limits(
        max_images=int(limits_spec.get("max_images", Limits.max_images)),
        max_image_bytes=int(limits_spec.get("max_image_bytes", Limits.max_image_bytes)),
        build_timeout=timedelta(seconds=int(limits_spec.get("build_timeout_s", int(Limits.build_timeout.total_seconds())))),
        idle_days=int(limits_spec.get("idle_days", Limits.idle_days)),
    )
    builder_spec = dict(spec.get("builder") or {})
    builder = None
    if builder_spec.get("kind") == "k8s":
        from agentruntime_core.adapters.kube import KubeAPI

        builder = builder_from_spec(builder_spec, KubeAPI.load(builder_spec.get("kubeconfig") or None))
    elif builder_spec:
        raise ValueError("environments.builder.kind 只能是 k8s")
    manager = EnvironmentManager(
        broker.ledger, base_image=str(spec["base_image"]), base_digest=str(spec.get("base_digest") or ""), registry=registry, builder=builder,
        pusher=LazyRegistry(str(registry_spec["url"]), registry.host, broker.sandboxes.read_secret), limits=limits,
        network=BuildNetwork(broker, tuple(str(h) for h in spec.get("build_hosts") or ())),
    )
    return manager


def temporal_starter(client: Any, *, timeout_s: int, task_queue: str = names.TASK_QUEUE, activity_queue: str = names.PROGRAM_QUEUE) -> BuildStarter:
    """把构建交给 Temporal：同一个（租户，键）已经在构建就沿用。"""

    async def start(owner: str, key: str) -> None:
        await client.start_workflow(
            names.ENV_BUILD_WORKFLOW,
            EnvBuildInput(tenant_id=owner, key=key, timeout_s=timeout_s, activity_queue=activity_queue),
            id=build_workflow_id(owner, key),
            task_queue=task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )

    return start


__all__ = ["EnvBuildActivities", "LazyRegistry", "REGISTRY_SECRET", "environment_manager", "temporal_starter"]
