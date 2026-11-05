"""SBX-01：Worker 经 Sandbox Service 用沙箱（docs/plan/sbx-01-sandbox-service-2026-11-03.md 第 6、9 节）。

不按租户灰度（2026-11-04 用户纠正）：Worker 只有一条路，shell_run、repo_* 与 Workspace 操作都调服务；进程内的 Broker 与
py-core 的沙箱后端适配器在这一版删掉。这里对着真的服务进程（tests/sandbox_service_support.py）核对：

1. shell_run：同一会话的两条命令落在同一个环境（第二条标着复用），不同会话、没有会话时各用各的；一次性的环境用完销毁。
2. repo_read、repo_list、repo_grep、repo_edit 经服务，在工作区里读写；越界的路径拒绝。
3. 平台注入的凭据不进 Worker：Lease 上没有环境变量，只有测试用密钥的名字。
4. 故障注入：服务被杀（SIGKILL）再起来，同一会话的下一条命令照常落在原来的环境里。
5. Worker 不再依赖沙箱后端适配器：Broker、K8s、Cloudflare、kube、sandbox-agent 客户端的模块都不在了。
"""

from __future__ import annotations

import importlib.util
import io
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.tenancy import TenantContext

from sandbox_service_support import WORKER_TOKEN, SandboxService

S1 = "ses_01HZY2J5K3M4N5P6Q7R8S9T0VW"
S2 = "ses_01HZY2J5K3M4N5P6Q7R8S9T0VX"


@pytest.fixture
def service(tmp_path: Path) -> Iterator[SandboxService]:
    svc = SandboxService(tmp_path / "svc")
    svc.start()
    try:
        yield svc
    finally:
        svc.stop()


class Tools:
    def __init__(self, svc: SandboxService) -> None:
        from agentruntime_worker.sandbox.client import ServiceBroker
        from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools

        self.svc = svc
        self.broker = ServiceBroker.connect(svc.address, WORKER_TOKEN)
        self.telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
        self.tools = LocalToolDriverProvider()
        register_platform_tools(
            self.tools, PlatformToolsConfig(shell_sandbox="sandbox", session_sandboxes=True, workspace_tools=True),
            sandboxes=self.broker, telemetry=self.telemetry,
        )

    async def call(self, tool: str, args: dict[str, Any], *, session: str = S1, workspace: str = "w1") -> dict[str, Any]:
        (self.svc.workspaces / "t1" / workspace).mkdir(parents=True, exist_ok=True)
        tenant = TenantContext(tenant_id="t1", user_id="u1", workspace_id=workspace, session_id=session)
        out = (await self.tools.bind(tenant).invoke(tool, "v1", args, idempotency_key=f"idk-{os.urandom(4).hex()}")).output
        await self.telemetry.bind(tenant).flush()
        return out

    def spans(self) -> list[dict[str, Any]]:
        return [dict(s.attributes) for s in self.telemetry.exported_spans if s.name == "runtime.sandbox"]


async def test_shell_run_reuses_the_session_environment_through_the_service(service: SandboxService) -> None:
    t = Tools(service)
    first = await t.call("shell_run", {"argv": ["sh", "-c", "echo one > a.txt; echo ok"]})
    second = await t.call("shell_run", {"argv": ["sh", "-c", "cat a.txt"]})
    assert first["exit_code"] == 0 and second["stdout"].strip() == "one"
    spans = t.spans()
    assert spans[0]["sandbox.env_ref"] == spans[1]["sandbox.env_ref"] and spans[1]["sandbox.reused"] is True
    other = await t.call("shell_run", {"argv": ["sh", "-c", "echo hi"]}, session=S2, workspace="w2")
    assert t.spans()[-1]["sandbox.env_ref"] != spans[0]["sandbox.env_ref"] and other["exit_code"] == 0
    assert (service.workspaces / "t1" / "w1" / "a.txt").read_text() == "one\n", "挂卷：命令写进了工作区"


async def test_one_shot_shell_run_without_a_session(service: SandboxService) -> None:
    t = Tools(service)
    out = await t.call("shell_run", {"argv": ["sh", "-c", "echo once"]}, session="")
    assert out["stdout"].strip() == "once"
    assert t.spans()[-1]["sandbox.reused"] is False


async def test_repo_tools_go_through_the_service(service: SandboxService) -> None:
    t = Tools(service)
    root = service.workspaces / "t1" / "w1"
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def main():\n    return 1\n", "utf-8")
    listed = await t.call("repo_list", {"path": "src"})
    assert [e["name"] for e in listed["entries"]] == ["app.py"]
    read = await t.call("repo_read", {"path": "src/app.py"})
    assert "return 1" in read["content"]
    found = await t.call("repo_grep", {"pattern": "return"})
    assert found["matches"][0]["path"] == "src/app.py"
    await t.call("repo_edit", {"path": "src/app.py", "old": "return 1", "new": "return 2"})
    assert "return 2" in (root / "src" / "app.py").read_text("utf-8")
    from agentruntime_core.errors import ProviderError

    with pytest.raises(ProviderError):
        await t.call("repo_read", {"path": "../../t2/w1/secret"})


async def test_platform_credentials_never_reach_the_worker(tmp_path: Path) -> None:
    svc = SandboxService(tmp_path / "svc", env={"AR_PKG_MIRROR_URL": "http://pkg-mirror.test:8093"})
    secrets = __import__("json").loads(svc.secrets_file.read_text())
    secrets["scopes"]["_platform"]["secrets"]["sandbox/env-credential-key"] = "ZW52LWNyZWRlbnRpYWwta2V5LTAxMjM0NTY3ODlhYmNkZWY="
    svc.secrets_file.write_text(__import__("json").dumps(secrets))
    svc.start()
    try:
        from agentruntime_core.providers.sandbox import SandboxSpec

        from agentruntime_worker.sandbox.client import ServiceBroker

        broker = ServiceBroker.connect(svc.address, WORKER_TOKEN)
        (svc.workspaces / "t1" / "w1").mkdir(parents=True)
        tenant = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1")
        lease = await broker.acquire(tenant, SandboxSpec(workspace_id="w1"))
        result = await lease.box.exec(lease.env_id, ["sh", "-c", 'printf %s "$PIP_INDEX_URL"'], timeout=__import__("datetime").timedelta(seconds=30))
        assert result.stdout.decode().startswith("http://pkg-mirror.test:8093/t/ar1."), "命令拿到了包镜像的凭据"
        assert not getattr(lease, "env", None), "Worker 这边看不到平台注入的环境变量"
        assert "ar1." not in repr(lease)
        await broker.release(lease)
    finally:
        svc.stop()


async def test_service_killed_mid_session_keeps_the_environment(service: SandboxService) -> None:
    t = Tools(service)
    await t.call("shell_run", {"argv": ["sh", "-c", "echo kept > state.txt"]})
    ref = t.spans()[-1]["sandbox.env_ref"]
    service.restart()  # SIGKILL 再起来
    out = await t.call("shell_run", {"argv": ["sh", "-c", "cat state.txt"]})
    assert out["stdout"].strip() == "kept"
    assert t.spans()[-1]["sandbox.env_ref"] == ref and t.spans()[-1]["sandbox.reused"] is True


def test_the_worker_no_longer_ships_sandbox_backends() -> None:
    gone = [
        "agentruntime_worker.sandbox.broker", "agentruntime_worker.sandbox.ledger", "agentruntime_worker.sandbox.reconcile",
        "agentruntime_worker.sandbox.env_builder", "agentruntime_worker.tools.sandboxes",
        "agentruntime_core.adapters.k8s_sandbox", "agentruntime_core.adapters.cloudflare_sandbox", "agentruntime_core.adapters.kube",
        "agentruntime_core.adapters.sandbox_agent",
    ]
    present = [m for m in gone if importlib.util.find_spec(m) is not None]
    assert not present, f"Worker 不再有进程内的 Broker 与沙箱后端适配器：{present}"
