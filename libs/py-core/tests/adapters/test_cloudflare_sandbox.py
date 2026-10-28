"""D-07：Sandbox 的 Cloudflare 实现（经桥在 Cloudflare Containers 里建环境；docs/plan/cloud-deploy.md）。

- 对着本机的假桥（cloudflaretest：同一套接口，每个环境一个真的 sandbox-agent 进程）跑与开发实现相同的一致性测试。
- 桥的令牌不对时 denied；按域名放行时把 allow_hosts 交给桥，主机名格式不对的 invalid；不能挂 workspace（invalid），
  工作区按存档模式用（D-06：存档与恢复、列目录与搜索也在一致性测试里）。
- 设 AR_LIVE_CLOUDFLARE=1（另要 AR_CLOUDFLARE_BRIDGE_URL、AR_CLOUDFLARE_BRIDGE_TOKEN）时对真实的桥再跑一遍一致性测试，
  并验证 allowlist 之外的主机连不上（花钱、不进 CI）。
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import pytest

from agentruntime_core.adapters import cloudflaretest
from agentruntime_core.adapters.cloudflare_sandbox import CloudflareSandboxProvider
from agentruntime_core.conformance.common import expect_error, tenant_a
from agentruntime_core.conformance.sandbox import SandboxArchiveConformance, SandboxConformance
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.testing import FaultInjector

LIVE = os.environ.get("AR_LIVE_CLOUDFLARE") == "1"


@pytest.fixture(scope="module")
def bridge():
    try:
        b = cloudflaretest.FakeBridge()
    except cloudflaretest.MissingTool as err:
        pytest.fail(str(err), pytrace=False)
    yield b
    b.stop()


class Harness:
    def __init__(self, url: str, token: str) -> None:
        self.faults = FaultInjector()
        self.provider = CloudflareSandboxProvider(url, token, faults=self.faults)

    async def advance(self, delta: timedelta) -> None:
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)


class TestCloudflareSandboxAgainstTheFakeBridge(SandboxConformance, SandboxArchiveConformance):
    @pytest.fixture
    def harness(self, bridge: cloudflaretest.FakeBridge) -> Harness:
        return Harness(bridge.url, bridge.token)


@pytest.mark.skipif(not LIVE, reason="设 AR_LIVE_CLOUDFLARE=1 才对真实的桥跑（花钱）")
class TestCloudflareSandboxLive(SandboxConformance, SandboxArchiveConformance):
    @pytest.fixture
    def harness(self) -> Harness:
        return Harness(os.environ["AR_CLOUDFLARE_BRIDGE_URL"], os.environ["AR_CLOUDFLARE_BRIDGE_TOKEN"])


async def test_wrong_bridge_token_is_denied(bridge: cloudflaretest.FakeBridge) -> None:
    provider = CloudflareSandboxProvider(bridge.url, "x" * 48)
    with expect_error(ErrorCategory.DENIED):
        await provider.bind(tenant_a()).create(SandboxSpec())


async def test_allowlist_is_passed_to_the_bridge(bridge: cloudflaretest.FakeBridge) -> None:
    box = CloudflareSandboxProvider(bridge.url, bridge.token).bind(tenant_a())
    env = await box.create(SandboxSpec(network="allowlist", allow_hosts=("pypi.org", "*.pythonhosted.org"), idle_timeout=timedelta(minutes=5)))
    assert bridge.created[-1]["allow_hosts"] == ["pypi.org", "*.pythonhosted.org"]
    assert bridge.created[-1]["idle_seconds"] == 300
    await box.destroy(env.env_id)


@pytest.mark.parametrize("hosts", [(), ("not a host",), ("https://pypi.org",), ("x" * 300 + ".com",)])
async def test_bad_allow_hosts_are_invalid(bridge: cloudflaretest.FakeBridge, hosts: tuple[str, ...]) -> None:
    box = CloudflareSandboxProvider(bridge.url, bridge.token).bind(tenant_a())
    with expect_error(ErrorCategory.INVALID):
        await box.create(SandboxSpec(network="allowlist", allow_hosts=hosts))


async def test_workspace_cannot_be_mounted(bridge: cloudflaretest.FakeBridge) -> None:
    """工作区不挂卷：按存档模式用（D-06，Worker 建环境时 workspace_id 置空）。"""
    box = CloudflareSandboxProvider(bridge.url, bridge.token).bind(tenant_a())
    with expect_error(ErrorCategory.INVALID, "unsupported_workspace"):
        await box.create(SandboxSpec(workspace_id="w1"))


async def test_runtimes_follow_the_bridge_image(bridge: cloudflaretest.FakeBridge) -> None:
    """桥的镜像在部署时定：默认只有 python；登记成 universal 镜像的桥也接 universal（里面有 python），别的仍拒绝。"""
    default = CloudflareSandboxProvider(bridge.url, bridge.token).bind(tenant_a())
    with expect_error(ErrorCategory.INVALID, "unsupported_runtime"):
        await default.create(SandboxSpec(runtime="universal"))
    universal = CloudflareSandboxProvider(bridge.url, bridge.token, runtimes=("universal", "python")).bind(tenant_a())
    env = await universal.create(SandboxSpec(runtime="universal"))
    await universal.destroy(env.env_id)
    with expect_error(ErrorCategory.INVALID, "unsupported_runtime"):
        await universal.create(SandboxSpec(runtime="go"))


def test_bridge_must_be_https() -> None:
    with pytest.raises(ValueError):
        CloudflareSandboxProvider("http://example.com", "x" * 48)


@pytest.mark.skipif(not LIVE, reason="设 AR_LIVE_CLOUDFLARE=1 才对真实的桥跑（花钱）")
async def test_live_allowlist_blocks_other_hosts() -> None:
    box = CloudflareSandboxProvider(os.environ["AR_CLOUDFLARE_BRIDGE_URL"], os.environ["AR_CLOUDFLARE_BRIDGE_TOKEN"]).bind(tenant_a())
    env = await box.create(SandboxSpec(network="allowlist", allow_hosts=("pypi.org",)))
    code = (
        "import urllib.request\n"
        "for url in ['https://pypi.org/simple/', 'https://example.com/']:\n"
        "    try:\n"
        "        print(url, urllib.request.urlopen(url, timeout=10).status)\n"
        "    except Exception as e:\n"
        "        print(url, 'blocked', type(e).__name__)\n"
    )
    out = (await box.exec(env.env_id, ["python", "-c", code], timeout=timedelta(seconds=60))).stdout.decode()
    await box.destroy(env.env_id)
    assert "https://pypi.org/simple/ 200" in out and "https://example.com/ blocked" in out, out
