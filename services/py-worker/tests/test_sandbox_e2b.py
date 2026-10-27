"""e2b 分支 E-02：沙箱后端登记里的 kind = e2b（tools/sandbox_e2b.py；docs/plan/e2b-backend.md）。

- 登记文件读进来时就核对：要写明 isolation: microvm，要有 api_url，按头路由时要有 sandbox_url。
- API key 取自 SecretStore 平台作用域的 sandbox/e2b-api-key；没有时 invalid（missing_e2b_api_key），不带着空 key 去试。
- 建出来的后端对着假 E2B 能建环境、跑命令；按租户策略选到它。
- shell_run 绑定 workspace：要等 D-06，报 unsupported_workspace（与 Cloudflare 后端一样）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.adapters import e2btest
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.errors import InvalidError
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.tools.sandboxes import Sandboxes

T1 = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1")


@pytest.fixture(scope="module")
def fake():
    f = e2btest.FakeE2B()
    yield f
    f.stop()


def spec_for(fake: e2btest.FakeE2B, **extra: Any) -> dict[str, Any]:
    return {"kind": "e2b", "isolation": "microvm", "api_url": fake.api_url, "sandbox_url": fake.sandbox_url, "deployment": "worker-test", **extra}


def secrets_with_key(tmp_path: Path, key: str | None) -> Path:
    path = tmp_path / "secrets.json"
    provider = LocalFileSecretStoreProvider(path)
    provider.seed_secret(None, "sandbox/token-key", "k" * 64)
    if key is not None:
        provider.seed_secret(None, "sandbox/e2b-api-key", key)
    return path


def policy_from(policies: Mapping[str, Mapping[str, Any]]):
    async def lookup(tenant_id: str) -> Mapping[str, Any] | None:
        return policies.get(tenant_id)

    return lookup


async def test_e2b_backend_runs_commands(tmp_path: Path, fake: e2btest.FakeE2B) -> None:
    boxes = Sandboxes(
        {"gvisor": {"kind": "local", "isolation": "none"}, "e2b": spec_for(fake)},
        "gvisor",
        secrets_file=secrets_with_key(tmp_path, fake.api_key),
        policy=policy_from({"t1": {"sandbox": {"backend": "e2b"}}}),
    )
    backend = await boxes.for_tenant("t1")
    assert backend.name == "e2b" and backend.kind == "e2b" and backend.isolation == "microvm"
    box = backend.provider.bind(T1)
    env = await box.create(SandboxSpec())
    out = await box.exec(env.env_id, ["python", "-c", "print(6 * 7)"], timeout=timedelta(seconds=30))
    assert out.stdout.strip() == b"42"
    await box.destroy(env.env_id)
    assert fake.creates[-1]["metadata"]["agentruntime.deployment"] == "worker-test"
    await boxes.aclose()


async def test_missing_api_key_is_invalid(tmp_path: Path, fake: e2btest.FakeE2B) -> None:
    boxes = Sandboxes({"e2b": spec_for(fake)}, "e2b", secrets_file=secrets_with_key(tmp_path, None))
    with pytest.raises(InvalidError) as info:
        await boxes.backend("e2b")
    assert info.value.code == "missing_e2b_api_key"


@pytest.mark.parametrize(
    ("change", "what"),
    [
        ({"isolation": "container"}, "isolation"),
        ({"isolation": None}, "isolation"),
        ({"api_url": ""}, "api_url"),
        ({"sandbox_url": ""}, "sandbox_url"),
        ({"templates": {"python": ""}}, "templates"),
    ],
)
def test_backends_file_checks_e2b_entries(tmp_path: Path, change: dict[str, Any], what: str) -> None:
    entry = {"kind": "e2b", "isolation": "microvm", "api_url": "http://e2b:3000", "sandbox_url": "http://e2b:3002"}
    for k, v in change.items():
        if v is None:
            entry.pop(k)
        else:
            entry[k] = v
    path = tmp_path / "backends.json"
    path.write_text(json.dumps({"default": "e2b", "backends": {"e2b": entry}}), "utf-8")
    with pytest.raises(ValueError, match=what):
        Sandboxes.from_file(path)


async def test_shell_run_with_a_workspace_waits_for_d06(tmp_path: Path, fake: e2btest.FakeE2B) -> None:
    workspaces = tmp_path / "workspaces"
    boxes = Sandboxes({"e2b": spec_for(fake)}, "e2b", workspace_root=workspaces, secrets_file=secrets_with_key(tmp_path, fake.api_key))
    provider = LocalToolDriverProvider()
    register_platform_tools(provider, PlatformToolsConfig(workspace_root=workspaces, shell_sandbox="sandbox"), sandboxes=boxes)
    with pytest.raises(InvalidError) as info:
        await provider.bind(T1).invoke("shell_run", "v1", {"argv": ["python", "-c", "print(1)"]}, idempotency_key=f"idk-{os.urandom(4).hex()}")
    assert info.value.code == "unsupported_workspace"
    await boxes.aclose()
