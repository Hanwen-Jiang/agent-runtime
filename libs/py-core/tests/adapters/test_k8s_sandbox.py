"""D-05：Sandbox 的 K8s 实现（每个环境一个一次性 Pod，agent 在 Pod 里执行；docs/plan/cloud-deploy.md）。

- 与开发实现同一套一致性测试，含绑定 workspace 的规则（工作区经 deploy/cluster/workspaces.yaml 的 PV、PVC 挂进沙箱）。
- 集群约束（deploy/cluster/sandboxes.yaml）：沙箱以 10001 跑、根文件系统只读、没有 ServiceAccount 令牌、连不上外网与集群内服务。
- 令牌：agent 不接受别的令牌；令牌由 token_key 派生，换了 token_key 的进程连不上。
- 空闲到期的 Pod 被回收；按域名放行的网络请求 invalid。

用 kind 拉起的集群（kindtest，经 API Server 的 pods/proxy 连 agent）。gVisor 与 Kata 在 kind 里没有，留到真机器上验。
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.adapters import kindtest
from agentruntime_core.adapters.k8s_sandbox import LABELS, K8sPodSandboxProvider, K8sSandboxConfig
from agentruntime_core.adapters.kube import KubeAPI, label_selector
from agentruntime_core.conformance.common import expect_error, tenant_a, tenant_b
from agentruntime_core.conformance.sandbox import SandboxWithWorkspaceConformance
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.testing import FaultInjector

TIMEOUT = timedelta(seconds=60)


def make_provider(
    cluster: kindtest.Cluster, faults: FaultInjector | None = None, *, token_key: bytes = b"k" * 32, **overrides: Any
) -> K8sPodSandboxProvider:
    config = K8sSandboxConfig(
        namespace="sandboxes",
        images={"python": cluster.python_image},
        agent_image=cluster.agent_image,
        connect="api_proxy",
        workspace_claim="workspaces",
        image_pull_policy="IfNotPresent",
        startup_timeout=timedelta(minutes=3),
        **overrides,
    )
    return K8sPodSandboxProvider(KubeAPI.from_kubeconfig(cluster.kubeconfig), config, token_key=token_key, faults=faults)


async def delete_all(cluster: kindtest.Cluster) -> None:
    kube = KubeAPI.from_kubeconfig(cluster.kubeconfig)
    try:
        await kube.request("DELETE", "/api/v1/namespaces/sandboxes/pods?labelSelector=" + label_selector(LABELS), {"gracePeriodSeconds": 0})
    finally:
        kube.close()


class Harness:
    def __init__(self, cluster: kindtest.Cluster) -> None:
        self.cluster = cluster
        self.faults = FaultInjector()
        self.provider = make_provider(cluster, self.faults)

    async def advance(self, delta: timedelta) -> None:
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)

    def workspace_path(self, tenant_id: str, workspace_id: str) -> Path:
        return self.cluster.workspace_path(tenant_id, workspace_id)


@pytest.fixture
async def harness(kind_cluster: kindtest.Cluster):
    h = Harness(kind_cluster)
    yield h
    await delete_all(kind_cluster)
    await h.provider.aclose()


class TestK8sPodSandbox(SandboxWithWorkspaceConformance):
    pass


async def run(harness: Harness, code: str) -> tuple[int | None, str, str]:
    box = harness.provider.bind(tenant_a())
    env = await box.create(SandboxSpec())
    res = await box.exec(env.env_id, ["python", "-c", code], timeout=TIMEOUT)
    return res.exit_code, res.stdout.decode().strip(), res.stderr.decode()


async def test_sandbox_runs_as_non_root_with_a_read_only_root(harness: Harness) -> None:
    code, out, _ = await run(harness, "import os; print(os.getuid())")
    assert out == "10001"
    code, out, err = await run(harness, "open('/etc/x', 'w')")
    assert code != 0 and ("Read-only file system" in err or "Permission denied" in err)


async def test_exec_reports_cpu_time_for_metering(harness: Harness) -> None:
    """D-10：agent 报出命令用掉的 CPU 时间，Worker 记进 runtime.sandbox span。"""
    box = harness.provider.bind(tenant_a())
    env = await box.create(SandboxSpec())
    res = await box.exec(env.env_id, ["python", "-c", "import time\nend = time.time() + 0.5\nwhile time.time() < end: pass"], timeout=TIMEOUT)
    assert res.cpu_time is not None and res.cpu_time >= timedelta(milliseconds=200), res.cpu_time


async def test_each_tenant_has_its_own_quota(harness: Harness) -> None:
    """D-10：一个租户同时最多 max_envs_per_scope 个环境，超了 denied；别的租户不受影响，销毁后又能建。"""
    limited = make_provider(harness.cluster, max_envs_per_scope=1)
    try:
        a, b = limited.bind(tenant_a()), limited.bind(tenant_b())
        first = await a.create(SandboxSpec())
        with expect_error(ErrorCategory.DENIED, "sandbox_quota_exceeded"):
            await a.create(SandboxSpec())
        other = await b.create(SandboxSpec())
        await a.destroy(first.env_id)
        await a.create(SandboxSpec())
        await b.destroy(other.env_id)
    finally:
        await limited.aclose()


async def test_sandbox_has_no_service_account_token(harness: Harness) -> None:
    _, out, _ = await run(harness, "import os; print(os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/token'))")
    assert out == "False"


async def test_sandbox_cannot_reach_the_network(harness: Harness) -> None:
    probe = (
        "import socket\n"
        "for host, port in [('1.1.1.1', 443), ('kubernetes.default.svc.cluster.local', 443), ('10.96.0.1', 443)]:\n"
        "    try:\n"
        "        socket.create_connection((host, port), timeout=3).close(); print('open', host)\n"
        "    except OSError:\n"
        "        print('blocked', host)\n"
    )
    _, out, _ = await run(harness, probe)
    assert "open" not in out, out


async def test_agent_only_accepts_its_own_token(harness: Harness) -> None:
    box = harness.provider.bind(tenant_a())
    env = await box.create(SandboxSpec())
    other = make_provider(harness.cluster, token_key=b"x" * 32)
    try:
        with expect_error(ErrorCategory.DENIED):
            await other.bind(tenant_a()).exec(env.env_id, ["python", "-c", "print(1)"], timeout=TIMEOUT)
    finally:
        await other.aclose()


async def test_network_allowlist_needs_a_microvm_backend(harness: Harness) -> None:
    with expect_error(ErrorCategory.INVALID, "network_allowlist_unsupported"):
        await harness.provider.bind(tenant_a()).create(SandboxSpec(network="allowlist", allow_hosts=("pypi.org",)))


async def test_idle_environments_are_reaped(harness: Harness) -> None:
    box = harness.provider.bind(tenant_a())
    env = await box.create(SandboxSpec(idle_timeout=timedelta(seconds=1)))
    await asyncio.sleep(2)
    assert await harness.provider.reap(force=True) >= 1
    with expect_error(ErrorCategory.NOT_FOUND):
        await box.exec(env.env_id, ["python", "-c", "print(1)"], timeout=TIMEOUT)


async def test_pod_spec_is_locked_down(harness: Harness) -> None:
    box = harness.provider.bind(tenant_a())
    env = await box.create(SandboxSpec(cpu_millis=500, memory_mb=256))
    pod = await harness.provider.kube.json("GET", harness.provider.pods_path(env.env_id))
    spec = pod["spec"]
    assert spec["automountServiceAccountToken"] is False and spec["enableServiceLinks"] is False
    container = spec["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["resources"]["limits"]["memory"] == "256Mi"
    assert pod["metadata"]["labels"]["agentruntime.io/network"] == "none"
    token = next(e["value"] for e in container["env"] if e["name"] == "AGENT_TOKEN")
    assert token == harness.provider.token(env.env_id)
    assert "t-alpha" not in json.dumps(pod["metadata"]["labels"]), "标签里只有作用域的哈希，不直接写租户 ID"
