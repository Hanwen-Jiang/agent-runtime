"""D-15 的 Worker 一侧：沙箱里的命令拿到包镜像的地址与环境凭据（sandbox.credentials；docs/plan/sandbox-platform.md 第 3 节）。

- 配了 AR_PKG_MIRROR_URL 时，K8s 与本地后端的命令拿到 PIP_INDEX_URL、npm_config_registry、GOPROXY 等，凭据在路径前缀里；
  凭据解得出这个租户、这个环境，带着租户策略的 daily_mirror_miss_bytes。
- 凭据只经 exec 的 env 给到命令：不进 span、环境表。
- 托管后端默认不注入；登记里写了 pkg_mirror_url 的用它。
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentruntime_core import envcred
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.config import ENV_PKG_MIRROR_URL, WorkerConfig
from agentruntime_worker.sandbox.broker import SandboxBroker
from agentruntime_worker.sandbox.credentials import EnvCredentials
from agentruntime_worker.sandbox.ledger import SandboxLedger
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.tools.sandboxes import Sandboxes

KEY = bytes(range(32))
T1 = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1")
PRINT = "import os, json; print(json.dumps({k: os.environ.get(k) for k in ['PIP_INDEX_URL', 'npm_config_registry', 'GOPROXY', 'PIP_TRUSTED_HOST', 'npm_config_audit']}))"


def make(tmp_path: Path, specs: Mapping[str, Any], default: str, policy: Mapping[str, Any] | None = None) -> tuple[LocalToolDriverProvider, ConsoleTelemetryProvider, SandboxLedger]:
    (tmp_path / "workspaces" / "t1" / "w1").mkdir(parents=True, exist_ok=True)

    async def lookup(tenant_id: str) -> Mapping[str, Any] | None:
        return policy

    async def key() -> bytes:
        return KEY

    boxes = Sandboxes(dict(specs), default, workspace_root=tmp_path / "workspaces", policy=lookup)
    ledger = SandboxLedger(SQLiteRelationalStoreProvider(tmp_path / "relational"))
    broker = SandboxBroker(boxes, ledger, credentials=EnvCredentials(key, mirror_url="http://pkg-mirror.test:8093/"))
    telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
    tools = LocalToolDriverProvider()
    register_platform_tools(tools, PlatformToolsConfig(workspace_root=tmp_path / "workspaces", shell_sandbox="sandbox"), sandboxes=broker, telemetry=telemetry)
    return tools, telemetry, ledger


async def run(tools: LocalToolDriverProvider) -> dict[str, Any]:
    out = (await tools.bind(T1).invoke("shell_run", "v1", {"argv": ["python", "-c", PRINT]}, idempotency_key="idk-mirror")).output
    return json.loads(out["stdout"])


async def test_commands_get_the_mirror_and_a_credential_for_their_environment(tmp_path: Path) -> None:
    tools, telemetry, ledger = make(tmp_path, {"local": {"kind": "local", "isolation": "none"}}, "local",
                                    {"sandbox": {"backend": "local", "quota": {"daily_mirror_miss_bytes": 1073741824}}})
    seen = await run(tools)
    index = seen["PIP_INDEX_URL"]
    match = re.fullmatch(r"http://pkg-mirror\.test:8093/t/(ar1\.[^/]+)/pypi/simple/", index)
    assert match, index
    token = match.group(1)
    assert seen["npm_config_registry"] == f"http://pkg-mirror.test:8093/t/{token}/npm/" and seen["GOPROXY"] == f"http://pkg-mirror.test:8093/t/{token}/go"
    assert seen["PIP_TRUSTED_HOST"] == "pkg-mirror.test" and seen["npm_config_audit"] == "false"
    claims = envcred.verify(KEY, token, purpose="mirror", now=datetime.now(UTC))
    await telemetry.bind(T1).flush()
    [span] = [s for s in telemetry.exported_spans if s.name == "runtime.sandbox"]
    assert claims.tenant_id == "t1" and claims.env_ref == span.attributes["sandbox.env_ref"] and claims.workspace_id == "w1"
    assert claims.limits == {"mirror_miss_bytes": 1073741824}, "限额取自租户策略"
    assert token not in json.dumps(dict(span.attributes), ensure_ascii=False, default=str), "凭据不进 span"
    record = await ledger.get("t1", claims.env_ref)
    assert record is not None and token not in json.dumps(record.__dict__, default=str), "凭据不进环境表"


async def test_hosted_backends_get_no_mirror_unless_configured(tmp_path: Path) -> None:
    specs = {"remote": {"kind": "local", "isolation": "none", "pkg_mirror_url": "https://mirror.example.com"}}
    tools, _, _ = make(tmp_path / "a", specs, "remote")
    assert (await run(tools))["PIP_INDEX_URL"].startswith("https://mirror.example.com/t/ar1."), "登记里的 pkg_mirror_url 优先"
    assert "PIP_TRUSTED_HOST" not in {k for k, v in (await run(tools)).items() if v}, "https 时不加 trusted-host"
    credentials = EnvCredentials(lambda: None, mirror_url="http://pkg-mirror:8093")  # type: ignore[arg-type,return-value]
    assert credentials.mirror_for({"kind": "cloudflare"}) == "", "托管后端默认不注入（断网时连不到集群里的镜像）"
    assert credentials.mirror_for({"kind": "k8s"}) == "http://pkg-mirror:8093"


def test_mirror_url_comes_from_the_environment() -> None:
    assert WorkerConfig.from_env({ENV_PKG_MIRROR_URL: "http://ar-agent-runtime-pkg-mirror:8093"}).pkg_mirror_url == "http://ar-agent-runtime-pkg-mirror:8093"
    assert WorkerConfig.from_env({}).pkg_mirror_url == ""
