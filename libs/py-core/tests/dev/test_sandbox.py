"""开发实现：本地子进程 Sandbox（仅限开发环境）。"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest

from agentruntime_core.conformance.common import expect_error
from agentruntime_core.conformance.sandbox import SandboxConformance, SandboxWorkspaceConformance
from agentruntime_core.dev.sandbox import LocalSubprocessSandboxProvider
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.tenancy import TenantContext
from agentruntime_core.testing import FaultInjector


class Harness:
    def __init__(self, root: Path) -> None:
        self.faults = FaultInjector()
        self.workspaces = root / "workspaces"
        self.provider = LocalSubprocessSandboxProvider(root / "envs", workspaces=self.workspaces, faults=self.faults)

    async def advance(self, delta: timedelta) -> None:
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)

    def workspace_path(self, tenant_id: str, workspace_id: str) -> Path:
        return self.workspaces / tenant_id / workspace_id


class TestLocalSubprocessSandbox(SandboxConformance, SandboxWorkspaceConformance):
    @pytest.fixture
    async def harness(self, tmp_path: Path):
        harness = Harness(tmp_path)
        yield harness
        await harness.provider.aclose()


async def test_workspace_needs_a_configured_root(tmp_path: Path) -> None:
    provider = LocalSubprocessSandboxProvider(tmp_path)
    with expect_error(ErrorCategory.INVALID, "unsupported_workspace"):
        await provider.bind(TenantContext(tenant_id="t1")).create(SandboxSpec(workspace_id="w1"))


async def test_code_in_the_sandbox_does_not_see_the_worker_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R-04 评审本期修第 1 条：节点代码拿不到长期密钥（设计书 4.12）。子进程只拿白名单里的环境变量，与 shell_run 相同。"""
    monkeypatch.setenv("AR_SECRETS_FILE", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("AR_MODEL_PROVIDER_API_KEY", "sk-must-not-leak")
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "tok-must-not-leak")
    provider = LocalSubprocessSandboxProvider(tmp_path / "sandbox")
    box = provider.bind(TenantContext(tenant_id="t1", user_id="u1"))
    env = await box.create(SandboxSpec(runtime="python", network="none"))
    res = await box.exec(env.env_id, ["python", "-c", "import json, os; print(json.dumps(dict(os.environ)))"], timeout=timedelta(seconds=60))
    assert res.exit_code == 0, res.stderr.decode("utf-8", "replace")
    seen = {k.upper(): v for k, v in json.loads(res.stdout).items()}
    assert not [k for k in seen if k.startswith("AR_")], "Worker 的 AR_* 变量不传给沙箱里的代码"
    assert "SOME_SERVICE_TOKEN" not in seen, "白名单以外的变量一律不传"
    assert "sk-must-not-leak" not in res.stdout.decode() and "tok-must-not-leak" not in res.stdout.decode()
    assert "PATH" in seen, "PATH 等运行必需的变量照传"
    await box.destroy(env.env_id)
    await provider.aclose()
