"""D-16 的 Worker 一侧：联网授权、request_network 工具、沙箱里的代理变量与测试用密钥（设计书改动稿 W8）。

网关本身（放行、SSRF、用量、审计、预览）的验收在 services/go-egress-gateway 的 go test；提交时的 network_request 在
task-service 的 taskapi/network_test.go。这里核对：
- 申请的规则（与 Go 一侧同一组用例）：只读不能申请；策略不允许、主机不在范围、时长超过上限 denied；IP 地址、内网名字不收。
- request_network：write_irreversible、要审批人（Node Executor 给的 approved_by），approver = admin 时提交者本人批的不算；
  批准的申请写进 sandbox_grants；预览端口给出预览地址与转发目标。
- 沙箱里的命令拿到经网关的代理变量（凭据用途 egress，环境与租户对得上），包镜像的主机不经代理；凭据不进 span 与环境表。
- 托管后端（没有网关）：Task 有授权时环境按域名放行，授权变了换环境；授权带用量上限时只落在经网关的后端。
- 测试用密钥：租户 SecretStore 的 sandbox/test-env 注入命令，span 只记名字。
- Task 的授权收回之后不再生效；租户删除时全部收回。
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from agentruntime_core import envcred
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.enums import Effect
from agentruntime_core.errors import DeniedError, InvalidError
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.config import ENV_EGRESS_GATEWAY_URL, ENV_PREVIEW_DOMAIN, WorkerConfig
from agentruntime_worker.sandbox.broker import SandboxBroker
from agentruntime_worker.sandbox.credentials import TEST_ENV_SECRET, EnvCredentials
from agentruntime_worker.sandbox.grants import GrantStore, NetworkPolicy, check_request, covers
from agentruntime_worker.sandbox.ledger import SandboxLedger
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.tools.sandboxes import Sandboxes

KEY = bytes(range(32))
SESSION = "ses_01HZY2J5K3M4N5P6Q7R8S9T0VW"
T1 = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1", session_id=SESSION)
TASK = "tsk_01HZY2J5K3M4N5P6Q7R8S9T0VW"
NETWORK = {"allowed": True, "hosts": ["api.example.com", "*.pypi.org"], "max_grant_minutes": 30, "preview": True}
PRINT = (
    "import os, json; print(json.dumps({k: os.environ.get(k) for k in "
    "['HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY', 'npm_config_https_proxy', 'PIP_INDEX_URL', 'STRIPE_TEST_KEY']}))"
)


def make(
    tmp_path: Path,
    *,
    network: Mapping[str, Any] | None = None,
    specs: Mapping[str, Any] | None = None,
    default: str = "local",
    fallback: list[str] | None = None,
    egress_url: str = "http://egress.test:8095",
    secrets_file: Path | None = None,
    sessions: bool = True,
) -> tuple[LocalToolDriverProvider, ConsoleTelemetryProvider, SandboxBroker]:
    (tmp_path / "workspaces" / "t1" / "w1").mkdir(parents=True, exist_ok=True)
    sandbox: dict[str, Any] = {"backend": default, "network": dict(network if network is not None else NETWORK)}
    if fallback:
        sandbox["fallback"] = fallback

    async def lookup(tenant_id: str) -> Mapping[str, Any] | None:
        return {"sandbox": sandbox}

    async def key() -> bytes:
        return KEY

    boxes = Sandboxes(dict(specs or {"local": {"kind": "local", "isolation": "none"}}), default, workspace_root=tmp_path / "workspaces",
                      policy=lookup, secrets_file=secrets_file)
    credentials = EnvCredentials(key, mirror_url="http://pkg-mirror.test:8093", egress_url=egress_url,
                                 test_env=lambda tenant: boxes.read_tenant_secret(tenant, TEST_ENV_SECRET))
    broker = SandboxBroker(boxes, SandboxLedger(SQLiteRelationalStoreProvider(tmp_path / "relational")), credentials=credentials)
    telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
    tools = LocalToolDriverProvider()
    register_platform_tools(tools, PlatformToolsConfig(
        workspace_root=tmp_path / "workspaces", shell_sandbox="sandbox", network_requests=bool(egress_url), preview_domain="preview.example.test",
        session_sandboxes=sessions,
    ), sandboxes=broker, telemetry=telemetry)
    return tools, telemetry, broker


async def request(tools: LocalToolDriverProvider, args: Mapping[str, Any], *, approved_by: str = "u-admin", key: str = "idk-net") -> Any:
    return (await tools.bind(T1).invoke("request_network", "v1", dict(args), idempotency_key=key, task_id=TASK, approved_by=approved_by)).output


async def shell(tools: LocalToolDriverProvider, *, key: str = "idk-shell") -> dict[str, Any]:
    out = (await tools.bind(T1).invoke("shell_run", "v1", {"argv": ["python", "-c", PRINT]}, idempotency_key=key, task_id=TASK)).output
    return json.loads(out["stdout"])


# --- 申请的规则（与 task-service 的 taskapi/network.go 同一组） ---


def test_request_rules_match_the_go_side() -> None:
    policy = NetworkPolicy.from_policy(NETWORK)
    ok = check_request(policy, hosts=["API.example.com", "files.pypi.org", "api.example.com"], minutes=15, reason=" 拉文档 ")
    assert ok.hosts == ("api.example.com", "files.pypi.org") and ok.ports == (80, 443) and ok.minutes == 15 and ok.reason == "拉文档"
    cases: list[tuple[dict[str, Any], type[Exception], str]] = [
        ({"hosts": ["evil.example.org"], "minutes": 15}, DeniedError, "host_not_allowed"),
        ({"hosts": ["*.example.com"], "minutes": 15}, DeniedError, "host_not_allowed"),
        ({"hosts": ["api.example.com"], "minutes": 60}, DeniedError, "grant_too_long"),
        ({"hosts": ["api.example.com"], "minutes": 20}, InvalidError, "invalid_network_request"),
        ({"hosts": ["169.254.169.254"], "minutes": 15}, InvalidError, "invalid_host"),
        ({"hosts": ["[::1]"], "minutes": 15}, InvalidError, "invalid_host"),
        ({"hosts": ["db.internal"], "minutes": 15}, DeniedError, "host_not_allowed"),
        ({"hosts": [], "minutes": 15}, InvalidError, "invalid_network_request"),
        ({"hosts": ["api.example.com"], "minutes": 15, "ports": [0]}, InvalidError, "invalid_network_request"),
        ({"hosts": ["api.example.com"], "minutes": 15, "effect_ceiling": "read"}, DeniedError, "network_read_only"),
    ]
    for kwargs, error, code in cases:
        with pytest.raises(error) as info:
            check_request(policy, **kwargs)
        assert info.value.code == code, kwargs
    with pytest.raises(DeniedError) as info:
        check_request(NetworkPolicy.from_policy({}), hosts=["api.example.com"], minutes=15)
    assert info.value.code == "network_not_allowed"
    with pytest.raises(DeniedError):
        check_request(NetworkPolicy.from_policy({**NETWORK, "preview": False}), hosts=["api.example.com"], minutes=15, preview_port=3000)
    assert covers("*.pypi.org", "files.pypi.org") and covers("*.pypi.org", "*.files.pypi.org") and not covers("*.pypi.org", "pypi.org")


# --- request_network ---


async def test_request_network_needs_an_approver_and_writes_the_grant(tmp_path: Path) -> None:
    tools, _, broker = make(tmp_path)
    spec = await tools.bind(T1).describe("request_network", "v1")
    assert spec.effect == Effect.WRITE_IRREVERSIBLE, "联网按不可逆写处理：只读的 Task 用不了，tool_loop 里逐次审批"
    with pytest.raises(DeniedError) as info:
        await request(tools, {"hosts": ["api.example.com"], "minutes": 15, "reason": "拉文档"}, approved_by="", key="k0")
    assert info.value.code == "network_not_approved"
    out = await request(tools, {"hosts": ["api.example.com"], "ports": [443], "minutes": 15, "reason": "拉文档"})
    assert out["hosts"] == ["api.example.com"] and out["ports"] == [443] and out["grant_id"].startswith("sbg-")
    assert broker.grants is not None
    [grant] = await broker.grants.for_task("t1", TASK)
    assert grant.approved_by == "u-admin" and grant.reason == "拉文档" and grant.session_id == SESSION
    expires = datetime.strptime(grant.expires_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    assert timedelta(minutes=14) < expires - datetime.now(UTC) <= timedelta(minutes=15)
    with pytest.raises(DeniedError) as info:
        await request(tools, {"hosts": ["evil.example.org"], "minutes": 15, "reason": "x"}, key="k2")
    assert info.value.code == "host_not_allowed"


async def test_admin_approver_rejects_self_approval(tmp_path: Path) -> None:
    tools, _, broker = make(tmp_path, network={**NETWORK, "approver": "admin"})
    with pytest.raises(DeniedError) as info:
        await request(tools, {"hosts": ["api.example.com"], "minutes": 15, "reason": "x"}, approved_by="u1", key="k1")
    assert info.value.code == "network_needs_admin"
    await request(tools, {"hosts": ["api.example.com"], "minutes": 15, "reason": "x"}, approved_by="u-admin", key="k2")
    assert broker.grants is not None and len(await broker.grants.for_task("t1", TASK)) == 1


async def test_request_network_is_only_registered_with_a_gateway_or_a_hosted_backend(tmp_path: Path) -> None:
    tools, _, _ = make(tmp_path / "a", egress_url="")
    assert "request_network" not in {s.name for s in await tools.bind(T1).list_tools()}, "没有网关、也没有托管后端：联网无从实现"
    hosted = {"local": {"kind": "local", "isolation": "none"}, "cf": {"kind": "cloudflare", "isolation": "microvm", "bridge_url": "https://b.test"}}
    tools, _, _ = make(tmp_path / "b", egress_url="", specs=hosted)
    assert "request_network" in {s.name for s in await tools.bind(T1).list_tools()}
    config = WorkerConfig.from_env({ENV_EGRESS_GATEWAY_URL: "http://egress:8095", ENV_PREVIEW_DOMAIN: "preview.example.com"})
    assert config.egress_gateway_url == "http://egress:8095" and config.preview_domain == "preview.example.com"


async def test_preview_port_gives_a_signed_url_for_the_session_environment(tmp_path: Path) -> None:
    tools, _, broker = make(tmp_path)
    out = await request(tools, {"hosts": ["api.example.com"], "minutes": 30, "reason": "OAuth 回调", "preview_port": 3000})
    url = urlsplit(out["preview_url"])
    env_ref = url.hostname.split(".")[0] if url.hostname else ""
    assert url.scheme == "https" and url.hostname == f"{env_ref}.preview.example.test" and re.fullmatch(r"sbe-[0-9a-f]{16}", env_ref)
    token = parse_qs(url.query)["ar_preview"][0]
    claims = envcred.verify(KEY, token, purpose="preview", now=datetime.now(UTC))
    assert claims.env_ref == env_ref and claims.tenant_id == "t1" and claims.limits == {"port": 3000}
    assert broker.grants is not None
    [grant] = await broker.grants.for_task("t1", TASK)
    assert grant.preview_env == env_ref and grant.preview_port == 3000 and grant.preview_target == "127.0.0.1:3000"
    record = await broker.ledger.get("t1", env_ref)  # type: ignore[union-attr]
    assert record is not None and record.session_id == SESSION and record.state == "ready", "预览开在本会话的环境上（环境留着）"
    # 每条命令一个环境时开不了预览（环境跑完就没了）。
    tools2, _, _ = make(tmp_path / "oneshot", sessions=False)
    with pytest.raises(InvalidError) as info:
        await request(tools2, {"hosts": ["api.example.com"], "minutes": 30, "reason": "x", "preview_port": 3000})
    assert info.value.code == "preview_unsupported"


# --- 沙箱里的代理变量与测试用密钥 ---


async def test_commands_get_the_gateway_proxy_with_an_egress_credential(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets.json"
    LocalFileSecretStoreProvider(secrets).seed_secret("t1", TEST_ENV_SECRET, json.dumps({"STRIPE_TEST_KEY": "sk_test_123"}))
    tools, telemetry, broker = make(tmp_path, secrets_file=secrets)
    seen = await shell(tools)
    proxy = seen["HTTPS_PROXY"]
    match = re.fullmatch(r"http://ar:(ar1\.[^@]+)@egress\.test:8095", proxy)
    assert match, proxy
    assert seen["HTTP_PROXY"] == proxy and seen["npm_config_https_proxy"] == proxy
    assert seen["NO_PROXY"].split(",")[0] == "pkg-mirror.test", "装依赖不经网关"
    claims = envcred.verify(KEY, match.group(1), purpose="egress", now=datetime.now(UTC))
    assert claims.tenant_id == "t1" and claims.workspace_id == "w1" and claims.session_id == SESSION
    assert seen["STRIPE_TEST_KEY"] == "sk_test_123", "租户的测试用密钥注入命令"
    await telemetry.bind(T1).flush()
    [span] = [s for s in telemetry.exported_spans if s.name == "runtime.sandbox"]
    attrs = json.dumps(dict(span.attributes), ensure_ascii=False, default=str)
    assert span.attributes["sandbox.network"] == "gateway" and span.attributes["sandbox.env_ref"] == claims.env_ref
    assert list(span.attributes["sandbox.test_env"]) == ["STRIPE_TEST_KEY"]
    assert "sk_test_123" not in attrs and match.group(1) not in attrs, "密钥与凭据不进 span"
    record = await broker.ledger.get("t1", claims.env_ref)  # type: ignore[union-attr]
    assert record is not None and "sk_test_123" not in json.dumps(record.__dict__) and match.group(1) not in json.dumps(record.__dict__)


async def test_test_env_cannot_override_the_proxy(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets.json"
    LocalFileSecretStoreProvider(secrets).seed_secret("t1", TEST_ENV_SECRET, json.dumps({"HTTPS_PROXY": "http://evil:1"}))
    tools, _, _ = make(tmp_path, secrets_file=secrets)
    with pytest.raises(InvalidError) as info:
        await shell(tools)
    assert info.value.code == "invalid_test_env" and "evil" not in str(info.value)


# --- 托管后端：按域名放行；要审计的授权只落在经网关的后端 ---

HOSTED = {
    "hosted": {"kind": "local", "isolation": "microvm", "egress_gateway": False},
    "cluster": {"kind": "local", "isolation": "microvm"},
}


async def test_hosted_backend_gets_an_allowlist_and_is_replaced_when_grants_change(tmp_path: Path) -> None:
    tools, _, broker = make(tmp_path, specs=HOSTED, default="hosted")
    first = await shell(tools, key="s1")
    assert first["HTTPS_PROXY"] is None, "托管后端没有网关，不注入代理"
    [env] = await broker.ledger.active("t1")  # type: ignore[union-attr]
    assert env.allow_hosts == "", "没有授权时断网"
    await request(tools, {"hosts": ["api.example.com"], "minutes": 15, "reason": "x"})
    await shell(tools, key="s2")
    active = await broker.ledger.active("t1")  # type: ignore[union-attr]
    assert [e.allow_hosts for e in active] == ['["api.example.com"]'], "授权之后换成按域名放行的环境，旧的回收"
    lease = await broker.acquire(T1, SandboxSpec(workspace_id="w1", session_id=SESSION), task_id=TASK, reuse=True)
    assert lease.network == "allowlist" and lease.reused and lease.record is not None and lease.record.allow_hosts == '["api.example.com"]'
    await broker.release(lease, keep=True)


async def test_audited_grants_only_land_on_gateway_backends(tmp_path: Path) -> None:
    audited = {**NETWORK, "max_up_bytes": 1048576}
    tools, _, broker = make(tmp_path, specs=HOSTED, default="hosted", fallback=["cluster"], network=audited)
    await request(tools, {"hosts": ["api.example.com"], "minutes": 15, "reason": "x"})
    seen = await shell(tools)
    assert seen["HTTPS_PROXY"] and seen["HTTPS_PROXY"].endswith("@egress.test:8095")
    [env] = await broker.ledger.active("t1")  # type: ignore[union-attr]
    assert env.backend == "cluster" and env.fallback_from == "hosted", "首选的托管后端做不到用量上限与审计，跳过"
    tools2, _, _ = make(tmp_path / "only-hosted", specs=HOSTED, default="hosted", network=audited)
    await request(tools2, {"hosts": ["api.example.com"], "minutes": 15, "reason": "x"})
    with pytest.raises(InvalidError) as info:
        await shell(tools2)
    assert info.value.code == "no_sandbox_backend" and "出网网关" in info.value.message


# --- 收回 ---


async def test_revoked_and_tenant_deleted_grants_stop_applying(tmp_path: Path) -> None:
    tools, _, broker = make(tmp_path)
    await request(tools, {"hosts": ["api.example.com"], "minutes": 15, "reason": "x"})
    store = broker.grants
    assert isinstance(store, GrantStore) and len(await store.for_task("t1", TASK)) == 1
    assert await store.revoke_task("t1", TASK) == 1 and await store.for_task("t1", TASK) == []
    await request(tools, {"hosts": ["api.example.com"], "minutes": 15, "reason": "again"}, key="idk-net-2")
    await shell(tools)
    assert await broker.destroy_tenant("t1") >= 1
    assert await store.for_task("t1", TASK) == [] and await broker.ledger.active("t1") == []  # type: ignore[union-attr]
    rows = await store.for_task("t1", TASK, active_only=False)
    assert {g.state for g in rows} == {"revoked"} and all(g.revoked_at for g in rows)
