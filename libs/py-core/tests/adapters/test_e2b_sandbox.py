"""e2b 分支：Sandbox 的自托管 E2B 实现（docs/plan/e2b-backend.md 第 3 节与第 6 节 E-01）。

- 对着本机的假 E2B（e2btest：api 与 client-proxy，envd 的命令是本机的真实子进程）跑与开发实现相同的一致性测试。
- 网络映射：none 断网、allowlist 放行名单并拒绝其余；沙箱端口一律要 traffic 令牌；配了出网网关时 egressProxy 带派生的凭据。
- 作用域由 metadata 核对：别的租户、别的部署是 not_found；换一个 Provider 实例（另一个 Worker 副本）照样能用自己的环境。
- 暂停后再用：自动 connect 恢复，文件还在；空闲期限每分钟最多顺延一次。
- 建沙箱的响应丢了时用同一个 Idempotency-Key 重试，不会多建；列出给对账时按部署过滤、跟着分页。
- 错误：API key 不对是 denied（消息里没有 key）；模板没有是 invalid；找不到命令是 invalid（command_not_found）。
- 设 AR_LIVE_E2B=1（另要 AR_E2B_API_URL、AR_E2B_SANDBOX_URL、AR_E2B_API_KEY，可选 AR_E2B_TEMPLATE）时对真实的自托管
  E2B 再跑一遍一致性测试，并验证断网（要 KVM 节点，不进 CI）。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from datetime import timedelta

import pytest

from agentruntime_core.adapters import e2btest
from agentruntime_core.adapters.e2b_sandbox import DEPLOYMENT_KEY, OWNER_KEY, E2BSandboxConfig, E2BSandboxProvider
from agentruntime_core.conformance.common import expect_error, tenant_a, tenant_b
from agentruntime_core.conformance.sandbox import SandboxConformance
from agentruntime_core.errors import ErrorCategory, TransientError
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.testing import FaultInjector

LIVE = os.environ.get("AR_LIVE_E2B") == "1"
TIMEOUT = timedelta(seconds=30)


@pytest.fixture(scope="module")
def fake():
    f = e2btest.FakeE2B()
    yield f
    f.stop()


def config(fake: e2btest.FakeE2B, **extra) -> E2BSandboxConfig:
    return E2BSandboxConfig(api_url=fake.api_url, sandbox_url=fake.sandbox_url, **extra)


class Harness:
    def __init__(self, cfg: E2BSandboxConfig, api_key: str) -> None:
        self.faults = FaultInjector()
        self.provider = E2BSandboxProvider(cfg, api_key, faults=self.faults)

    async def advance(self, delta: timedelta) -> None:
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)


class TestE2BSandboxAgainstTheFake(SandboxConformance):
    @pytest.fixture
    def harness(self, fake: e2btest.FakeE2B) -> Harness:
        return Harness(config(fake), fake.api_key)


def live_config() -> E2BSandboxConfig:
    template = os.environ.get("AR_E2B_TEMPLATE", "agentruntime-python")
    return E2BSandboxConfig(
        api_url=os.environ["AR_E2B_API_URL"], sandbox_url=os.environ["AR_E2B_SANDBOX_URL"], templates={"python": template}, deployment="live-test"
    )


@pytest.mark.skipif(not LIVE, reason="设 AR_LIVE_E2B=1 才对真实的自托管 E2B 跑（要 KVM 节点）")
class TestE2BSandboxLive(SandboxConformance):
    @pytest.fixture
    def harness(self) -> Harness:
        return Harness(live_config(), os.environ["AR_E2B_API_KEY"])


@pytest.mark.skipif(not LIVE, reason="设 AR_LIVE_E2B=1 才对真实的自托管 E2B 跑（要 KVM 节点）")
async def test_live_network_none_blocks_egress() -> None:
    box = E2BSandboxProvider(live_config(), os.environ["AR_E2B_API_KEY"]).bind(tenant_a())
    env = await box.create(SandboxSpec())
    code = "import urllib.request\ntry:\n    urllib.request.urlopen('https://example.com/', timeout=10)\n    print('open')\nexcept Exception as e:\n    print('blocked', type(e).__name__)\n"
    out = (await box.exec(env.env_id, ["python", "-c", code], timeout=timedelta(seconds=60))).stdout.decode()
    await box.destroy(env.env_id)
    assert "blocked" in out, out


async def test_network_none_and_ports_need_the_traffic_token(fake: e2btest.FakeE2B) -> None:
    box = E2BSandboxProvider(config(fake), fake.api_key).bind(tenant_a())
    env = await box.create(SandboxSpec(idle_timeout=timedelta(minutes=5)))
    req = fake.creates[-1]
    assert req["allow_internet_access"] is False and req["network"]["allowPublicTraffic"] is False
    assert req["timeout"] == 300 and req["autoPause"] is False and req["templateID"] == "agentruntime-python"
    assert "egressProxy" not in req["network"]
    await box.destroy(env.env_id)


async def test_allowlist_maps_to_allow_out_and_deny_the_rest(fake: e2btest.FakeE2B) -> None:
    box = E2BSandboxProvider(config(fake), fake.api_key).bind(tenant_a())
    env = await box.create(SandboxSpec(network="allowlist", allow_hosts=("PyPI.org", "*.pythonhosted.org")))
    net = fake.creates[-1]["network"]
    assert net["allowOut"] == ["pypi.org", "*.pythonhosted.org"] and net["denyOut"] == ["0.0.0.0/0"]
    assert "allow_internet_access" not in fake.creates[-1]
    await box.destroy(env.env_id)


@pytest.mark.parametrize("hosts", [(), ("not a host",), ("https://pypi.org",), ("x" * 300 + ".com",)])
async def test_bad_allow_hosts_are_invalid(fake: e2btest.FakeE2B, hosts: tuple[str, ...]) -> None:
    box = E2BSandboxProvider(config(fake), fake.api_key).bind(tenant_a())
    with expect_error(ErrorCategory.INVALID, "invalid_allow_hosts"):
        await box.create(SandboxSpec(network="allowlist", allow_hosts=hosts))


async def test_egress_proxy_gets_derived_credentials(fake: e2btest.FakeE2B) -> None:
    key = b"k" * 32
    box = E2BSandboxProvider(config(fake, egress_proxy="egress.agent-runtime.svc:1080"), fake.api_key, token_key=key).bind(tenant_a())
    env = await box.create(SandboxSpec())
    req = fake.creates[-1]
    proxy = req["network"]["egressProxy"]
    assert proxy["address"] == "egress.agent-runtime.svc:1080" and proxy["username"].startswith("e2b-")
    assert proxy["password"] == hmac.new(key, b"egress:" + proxy["username"].encode(), hashlib.sha256).hexdigest()
    assert req["metadata"]["agentruntime.egress_user"] == proxy["username"]
    assert fake.api_key not in str(req), "E2B 的 API key 不能当出网凭据"
    await box.destroy(env.env_id)


async def test_metadata_records_scope_and_deployment(fake: e2btest.FakeE2B) -> None:
    box = E2BSandboxProvider(config(fake, deployment="prod-1"), fake.api_key).bind(tenant_a())
    env = await box.create(SandboxSpec())
    metadata = fake.creates[-1]["metadata"]
    assert metadata[OWNER_KEY] == tenant_a().tenant_id and metadata[DEPLOYMENT_KEY] == "prod-1"
    other_deployment = E2BSandboxProvider(config(fake, deployment="prod-2"), fake.api_key).bind(tenant_a())
    with expect_error(ErrorCategory.NOT_FOUND):
        await other_deployment.exec(env.env_id, ["python", "-c", "print(1)"], timeout=TIMEOUT)
    with expect_error(ErrorCategory.NOT_FOUND):
        await other_deployment.destroy(env.env_id)
    await box.destroy(env.env_id)


async def test_another_worker_replica_can_use_the_environment(fake: e2btest.FakeE2B) -> None:
    first = E2BSandboxProvider(config(fake), fake.api_key).bind(tenant_a())
    env = await first.create(SandboxSpec())
    await first.write_file(env.env_id, "note.txt", b"from replica 1")
    second = E2BSandboxProvider(config(fake), fake.api_key)
    out = await second.bind(tenant_a()).exec(env.env_id, ["python", "-c", "print(open('note.txt').read())"], timeout=TIMEOUT)
    assert out.stdout.strip() == b"from replica 1"
    with expect_error(ErrorCategory.NOT_FOUND):
        await second.bind(tenant_b()).read_file(env.env_id, "note.txt")
    await first.destroy(env.env_id)


async def test_paused_environment_resumes_on_use_with_its_files(fake: e2btest.FakeE2B) -> None:
    box = E2BSandboxProvider(config(fake, pause_on_idle=True), fake.api_key).bind(tenant_a())
    env = await box.create(SandboxSpec())
    assert fake.creates[-1]["autoPause"] is True
    await box.write_file(env.env_id, "kept.txt", b"still here")
    await box.pause(env.env_id)
    sandbox_id = env.env_id.removeprefix("e2b-")
    assert fake.sandboxes[sandbox_id].state == "paused"
    other = E2BSandboxProvider(config(fake, pause_on_idle=True), fake.api_key).bind(tenant_a())  # 另一个副本：缓存里没有
    assert await other.read_file(env.env_id, "kept.txt") == b"still here"
    assert fake.sandboxes[sandbox_id].state == "running"
    assert ("POST", f"/v2/sandboxes/{sandbox_id}/connect") in fake.calls
    await box.destroy(env.env_id)


async def test_idle_timeout_is_extended_at_most_once_a_minute(fake: e2btest.FakeE2B) -> None:
    provider = E2BSandboxProvider(config(fake), fake.api_key)
    box = provider.bind(tenant_a())
    env = await box.create(SandboxSpec(idle_timeout=timedelta(minutes=7)))
    sandbox = fake.sandboxes[env.env_id.removeprefix("e2b-")]
    for _ in range(3):
        await box.exec(env.env_id, ["python", "-c", "print(1)"], timeout=TIMEOUT)
    assert sandbox.timeouts == [], "刚建的环境一分钟内不顺延"
    provider._known[env.env_id].touched -= 120
    await box.exec(env.env_id, ["python", "-c", "print(1)"], timeout=TIMEOUT)
    await box.exec(env.env_id, ["python", "-c", "print(1)"], timeout=TIMEOUT)
    assert sandbox.timeouts == [420]
    await box.destroy(env.env_id)


async def test_lost_create_response_is_retried_with_the_same_idempotency_key(fake: e2btest.FakeE2B) -> None:
    provider = E2BSandboxProvider(config(fake), fake.api_key)
    real = provider.api
    lost = {"n": 0}

    async def flaky(method, path, body=None, **kwargs):
        result = await real(method, path, body, **kwargs)
        if method == "POST" and path == "/v2/sandboxes" and lost["n"] == 0:
            lost["n"] += 1
            raise TransientError("sandbox_unavailable", "响应在路上丢了")
        return result

    provider.api = flaky  # type: ignore[method-assign]
    before = len(fake.sandboxes)
    env = await provider.bind(tenant_a()).create(SandboxSpec())
    assert len(fake.sandboxes) == before + 1, "重试拿到的是第一次建的那个，没有多建"
    assert env.env_id.removeprefix("e2b-") in fake.sandboxes
    await provider.bind(tenant_a()).destroy(env.env_id)


async def test_list_environments_filters_by_deployment_and_follows_pages(fake: e2btest.FakeE2B) -> None:
    fake.page_size = 2
    try:
        mine = E2BSandboxProvider(config(fake, deployment="list-me"), fake.api_key)
        theirs = E2BSandboxProvider(config(fake, deployment="list-other"), fake.api_key)
        envs = [await mine.bind(tenant_a()).create(SandboxSpec()) for _ in range(2)] + [await mine.bind(tenant_b()).create(SandboxSpec())]
        foreign = await theirs.bind(tenant_a()).create(SandboxSpec())
        listed = await mine.list_environments()
        assert sorted(e.env_id for e in listed) == sorted(e.env_id for e in envs)
        assert {e.owner for e in listed} == {tenant_a().tenant_id, tenant_b().tenant_id}
        assert all(e.state == "running" and e.created_at is not None for e in listed)
        for env, tenant in zip(envs, (tenant_a(), tenant_a(), tenant_b()), strict=True):
            await mine.bind(tenant).destroy(env.env_id)
        await theirs.bind(tenant_a()).destroy(foreign.env_id)
    finally:
        fake.page_size = 100


async def test_wrong_api_key_is_denied_without_leaking_it(fake: e2btest.FakeE2B) -> None:
    wrong = "e2b_" + "0" * 40
    with expect_error(ErrorCategory.DENIED) as info:
        await E2BSandboxProvider(config(fake), wrong).bind(tenant_a()).create(SandboxSpec())
    assert wrong not in str(info.value) and fake.api_key not in str(info.value)


async def test_unknown_template_and_runtime_are_invalid(fake: e2btest.FakeE2B) -> None:
    box = E2BSandboxProvider(config(fake, templates={"python": "missing-template"}), fake.api_key).bind(tenant_a())
    with expect_error(ErrorCategory.INVALID, "unknown_template"):
        await box.create(SandboxSpec())
    with expect_error(ErrorCategory.INVALID, "unsupported_runtime"):
        await box.create(SandboxSpec(runtime="node"))


async def test_workspace_binding_waits_for_d06(fake: e2btest.FakeE2B) -> None:
    box = E2BSandboxProvider(config(fake), fake.api_key).bind(tenant_a())
    with expect_error(ErrorCategory.INVALID, "unsupported_workspace"):
        await box.create(SandboxSpec(workspace_id="w1"))


async def test_missing_command_is_invalid(fake: e2btest.FakeE2B) -> None:
    box = E2BSandboxProvider(config(fake), fake.api_key).bind(tenant_a())
    env = await box.create(SandboxSpec())
    with expect_error(ErrorCategory.INVALID, "command_not_found"):
        await box.exec(env.env_id, ["definitely-not-a-command-7c1"], timeout=TIMEOUT)
    await box.destroy(env.env_id)


@pytest.mark.parametrize(
    ("kwargs", "key"),
    [
        ({"api_url": ""}, "e2b_" + "x" * 40),
        ({"api_url": "ftp://e2b"}, "e2b_" + "x" * 40),
        ({"api_url": "http://e2b:3000", "sandbox_url": ""}, "e2b_" + "x" * 40),
        ({"api_url": "http://e2b:3000", "sandbox_url": "http://e2b:3002", "routing": "dns"}, "e2b_" + "x" * 40),
        ({"api_url": "http://e2b:3000", "sandbox_url": "http://e2b:3002", "deployment": "Prod"}, "e2b_" + "x" * 40),
        ({"api_url": "http://e2b:3000", "sandbox_url": "http://e2b:3002", "workdir": "relative"}, "e2b_" + "x" * 40),
        ({"api_url": "http://e2b:3000", "sandbox_url": "http://e2b:3002"}, "short"),
    ],
)
def test_config_is_validated(kwargs: dict, key: str) -> None:
    with pytest.raises(ValueError):
        E2BSandboxProvider(E2BSandboxConfig(**kwargs), key)


def test_host_routing_needs_no_sandbox_url() -> None:
    E2BSandboxProvider(E2BSandboxConfig(api_url="https://api.e2b.internal", routing="host"), "e2b_" + "x" * 40)
