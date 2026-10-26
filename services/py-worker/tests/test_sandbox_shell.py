"""D-05、D-08：shell_run 经 Sandbox 接口跑（AR_TOOL_SHELL_SANDBOX=sandbox；docs/plan/cloud-deploy.md）。

- 每条命令一个绑定本 workspace 的一次性环境：看得到工作区里的文件，写的文件留在工作区；拿不到 Worker 的环境变量；超时返回 timed_out。
- 用哪个后端由租户策略的 sandbox.backend 决定，没写用 default；策略要的后端没登记时拒绝，不悄悄换。结果里写着用的哪个后端。
- 配置：sandbox 时必须给后端登记文件；登记文件的格式不对时启动就报错。
- 设 AR_TEST_KIND=1 时再用 kind 跑一遍 K8s 后端（AR_KIND_REUSE=1 复用集群）：Worker 的工作区与沙箱 Pod 经 PV、PVC 看到同一个目录。
"""

from __future__ import annotations

import io
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.errors import InvalidError
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.config import ENV_SANDBOX_BACKENDS_FILE, ENV_TOOL_SHELL_SANDBOX, WorkerConfig
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.tools.sandboxes import Sandboxes, parse_cpu_millis, parse_memory_mb

T1 = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1")
T2 = TenantContext(tenant_id="t2", user_id="u2", workspace_id="w1")


def policy_from(policies: Mapping[str, Mapping[str, Any]]):
    async def lookup(tenant_id: str) -> Mapping[str, Any] | None:
        return policies.get(tenant_id)

    return lookup


def make(tmp_path: Path, policies: Mapping[str, Mapping[str, Any]] | None = None, telemetry: Any = None) -> LocalToolDriverProvider:
    workspaces = tmp_path / "workspaces"
    (workspaces / "t1" / "w1").mkdir(parents=True)
    (workspaces / "t1" / "w1" / "seed.txt").write_text("种子", "utf-8")
    boxes = Sandboxes(
        {"plain": {"kind": "local", "isolation": "none"}, "other": {"kind": "local", "isolation": "none"}},
        "plain",
        workspace_root=workspaces,
        policy=policy_from(policies or {}),
    )
    provider = LocalToolDriverProvider()
    register_platform_tools(provider, PlatformToolsConfig(workspace_root=workspaces, shell_sandbox="sandbox"), sandboxes=boxes, telemetry=telemetry)
    return provider


async def shell(provider: LocalToolDriverProvider, argv: list[str], tenant: TenantContext = T1, **extra: Any) -> dict[str, Any]:
    return (await provider.bind(tenant).invoke("shell_run", "v1", {"argv": argv, **extra}, idempotency_key=f"idk-{os.urandom(4).hex()}")).output


async def test_shell_run_goes_through_the_sandbox_bound_to_the_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AR_TEST_WORKER_SECRET", "worker-only-secret-9c1d")
    provider = make(tmp_path)
    code = (
        "import os; print(open('seed.txt', encoding='utf-8').read()); print(os.environ.get('AR_TEST_WORKER_SECRET', 'absent'));"
        "open('made.txt', 'w').write('x')"
    )
    out = await shell(provider, ["python", "-c", code])
    assert out["exit_code"] == 0 and out["sandbox"] == "plain"
    assert "种子" in out["stdout"] and "absent" in out["stdout"], out
    assert (tmp_path / "workspaces" / "t1" / "w1" / "made.txt").read_text() == "x", "写的文件留在工作区"


async def test_shell_run_in_the_sandbox_times_out(tmp_path: Path) -> None:
    out = await shell(make(tmp_path), ["python", "-c", "import time; time.sleep(30)"], timeout_s=1)
    assert out["timed_out"] and out["exit_code"] is None


async def test_tenant_policy_picks_the_backend(tmp_path: Path) -> None:
    provider = make(tmp_path, {"t2": {"sandbox": {"backend": "other"}}})
    assert (await shell(provider, ["python", "-c", "print(1)"]))["sandbox"] == "plain", "没写策略用 default"
    assert (await shell(provider, ["python", "-c", "print(1)"], T2))["sandbox"] == "other"


async def test_each_run_is_metered_in_a_span_without_the_command_or_output(tmp_path: Path) -> None:
    """D-07、D-10：一次运行一个 runtime.sandbox span，记后端、隔离强度、存活与执行时间；不记命令与输出。"""
    telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
    provider = make(tmp_path, telemetry=telemetry)
    await shell(provider, ["python", "-c", "print('secret-output-7f3a')"])
    await telemetry.bind(T1).flush()
    [span] = [s for s in telemetry.exported_spans if s.name == "runtime.sandbox"]
    attrs = span.attributes
    assert attrs["sandbox.backend"] == "plain" and attrs["sandbox.isolation"] == "none" and attrs["sandbox.kind"] == "local"
    assert attrs["tenant_id"] == "t1" and attrs["gen_ai.tool.name"] == "shell_run"
    assert attrs["sandbox.lifetime_ms"] >= attrs["sandbox.exec_ms"] >= 0 and attrs["sandbox.timed_out"] is False
    assert "secret-output-7f3a" not in json.dumps(attrs, ensure_ascii=False, default=str), "span 里不记命令与输出"


async def test_unknown_backend_in_policy_is_rejected(tmp_path: Path) -> None:
    provider = make(tmp_path, {"t1": {"sandbox": {"backend": "firecracker-x"}}})
    with pytest.raises(InvalidError) as info:
        await shell(provider, ["python", "-c", "print(1)"])
    assert info.value.code == "unknown_sandbox_backend"


def test_backends_file_is_validated(tmp_path: Path) -> None:
    path = tmp_path / "backends.json"
    path.write_text(json.dumps({"default": "a", "backends": {"a": {"kind": "local"}}}), "utf-8")
    assert Sandboxes.from_file(path).names == ("a",)
    for doc, what in [
        ({"default": "b", "backends": {"a": {"kind": "local"}}}, "default"),
        ({"default": "a", "backends": {"a": {"kind": "vm"}}}, "kind"),
        ({"default": "a", "backends": {"a": {"kind": "local", "isolation": "strong"}}}, "isolation"),
        ({"default": "A", "backends": {"A": {"kind": "local"}}}, "格式"),
        ({"default": "a"}, "backends"),
    ]:
        path.write_text(json.dumps(doc), "utf-8")
        with pytest.raises(ValueError, match=what):
            Sandboxes.from_file(path)


def test_sandbox_mode_needs_a_backends_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=ENV_SANDBOX_BACKENDS_FILE):
        WorkerConfig.from_env({ENV_TOOL_SHELL_SANDBOX: "sandbox"})
    config = WorkerConfig.from_env({ENV_TOOL_SHELL_SANDBOX: "sandbox", ENV_SANDBOX_BACKENDS_FILE: str(tmp_path / "b.json")})
    assert config.sandbox_backends_file == tmp_path / "b.json"
    with pytest.raises(ValueError, match="AR_SANDBOX_BACKENDS_FILE"):
        register_platform_tools(LocalToolDriverProvider(), PlatformToolsConfig(workspace_root=tmp_path, shell_sandbox="sandbox"))


def test_docker_style_limits_are_converted() -> None:
    assert parse_cpu_millis("2") == 2000 and parse_cpu_millis("0.5") == 500
    assert parse_memory_mb("2g") == 2048 and parse_memory_mb("512m") == 512


@pytest.mark.skipif(os.environ.get("AR_TEST_KIND") != "1", reason="设 AR_TEST_KIND=1 才用 kind 跑 K8s 后端")
async def test_shell_run_on_kind(tmp_path: Path) -> None:
    from agentruntime_core.adapters import kindtest

    cluster = kindtest.Cluster()
    try:
        backends = {
            "gvisor-like": {
                "kind": "k8s", "isolation": "container", "namespace": "sandboxes", "image": cluster.python_image,
                "agent_image": cluster.agent_image, "connect": "api_proxy", "workspace_claim": "workspaces", "kubeconfig": str(cluster.kubeconfig),
            }
        }
        boxes = Sandboxes(backends, "gvisor-like", workspace_root=cluster.workspaces)
        provider = LocalToolDriverProvider()
        register_platform_tools(provider, PlatformToolsConfig(workspace_root=cluster.workspaces, shell_sandbox="sandbox"), sandboxes=boxes)
        tenant = TenantContext(tenant_id="t-kind", workspace_id="w-shell")
        workspace = cluster.workspace_path("t-kind", "w-shell")
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "seed.txt").write_text("from-worker", "utf-8")
        out = await shell(provider, ["python", "-c", "import os; print(os.getuid(), open('seed.txt').read()); open('made.txt', 'w').write('pod')"], tenant)
        assert out["exit_code"] == 0 and "10001 from-worker" in out["stdout"], out
        assert (workspace / "made.txt").read_text() == "pod"
        await boxes.aclose()
    finally:
        cluster.stop()
