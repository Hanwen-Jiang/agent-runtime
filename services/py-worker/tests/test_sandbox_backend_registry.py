"""D-19 验收：后端登记的启用开关与能力（docs/plan/firecracker-backend.md 第 5 节 D-19）。

1. default 指向 enabled: false 的后端时 Worker 启动失败（加载登记时报错），原因里有后端名。
2. 租户策略 backend: gvisor（启用）走 gVisor；不写时走 default。
3. 把一个后端设为 enabled: false（排空）：新建被拒（denied，sandbox_backend_disabled，说明是哪个后端、是登记停用的）；
   已有的环境照常复用、exec、销毁；对账不把它当孤儿删、不记丢失。另一个 Worker 副本（只共用业务库与后端）读到的是改过的登记。
4. 回退规则不动：首选 firecracker、fallback [gvisor]、没写 min_isolation 时 firecracker 不可用就拒绝；写了 min_isolation: container
   才回退，环境表记着 fallback_from 与原因。停用的回退后端不接新环境（跳过并说明）。
5. capabilities 取值不认识、firecracker 没配 workspace: archive、enabled 不是布尔值，都在加载时失败；firecracker 的实现在 D-20，
   在那之前只能登记为 enabled: false。
6. 能力：登记里写的 capabilities 能查到；没写时为空。
（验收 6 的“一致性测试对每个启用的后端各跑一遍”：一致性测试按适配器在 libs/py-core 里跑，不按登记文件；这里核对的是 enabled_names。）
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.adapters import cloudflaretest
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.errors import DeniedError, ErrorCategory, TransientError
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.broker import SandboxBroker
from agentruntime_worker.sandbox.ledger import SandboxLedger
from agentruntime_worker.sandbox.reconcile import reconcile_once
from agentruntime_worker.tools.sandboxes import CAPABILITIES, KINDS, Sandboxes

S1 = "ses_01HZY2J5K3M4N5P6Q7R8S9T0VW"
S2 = "ses_01HZY2J5K3M4N5P6Q7R8S9T0VX"


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def lookup_for(policy: Mapping[str, Any] | None):  # type: ignore[no-untyped-def]
    async def lookup(tenant_id: str) -> Mapping[str, Any] | None:
        return policy if tenant_id == "t1" else None

    return lookup


def broker(tmp_path: Path, specs: Mapping[str, Mapping[str, Any]], default: str, policy: Mapping[str, Any] | None = None, *,
           secrets: Path | None = None, relational: Path | None = None, clock: Clock | None = None) -> SandboxBroker:
    boxes = Sandboxes(dict(specs), default, workspace_root=tmp_path / "workspaces", secrets_file=secrets, policy=lookup_for(policy))
    return SandboxBroker(boxes, SandboxLedger(SQLiteRelationalStoreProvider(relational or tmp_path / "relational"), clock=clock or Clock()))


def load(tmp_path: Path, doc: Mapping[str, Any]) -> Sandboxes:
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(doc), "utf-8")
    return Sandboxes.from_file(path)


FIRECRACKER_OFF = {"kind": "firecracker", "isolation": "microvm", "workspace": "archive", "enabled": False,
                   "capabilities": ["snapshot", "block_attach", "vsock"]}


# --- 1、5、6：加载时的校验与能力 ---


def test_default_pointing_at_a_disabled_backend_fails_to_load(tmp_path: Path) -> None:
    doc = {"default": "gvisor", "backends": {"gvisor": {"kind": "local", "isolation": "container", "enabled": False}}}
    with pytest.raises(ValueError, match="gvisor"):
        load(tmp_path, doc)


@pytest.mark.parametrize(
    ("backend", "why"),
    [
        ({"kind": "local", "isolation": "container", "capabilities": ["teleport"]}, "capabilities"),
        ({"kind": "local", "isolation": "container", "capabilities": "snapshot"}, "capabilities"),
        ({"kind": "local", "isolation": "container", "enabled": "no"}, "enabled"),
        ({**FIRECRACKER_OFF, "workspace": "mount"}, "archive"),
        ({**FIRECRACKER_OFF, "enabled": True}, "D-20"),
    ],
)
def test_bad_registry_entries_fail_to_load(tmp_path: Path, backend: dict[str, Any], why: str) -> None:
    doc = {"default": "local", "backends": {"local": {"kind": "local", "isolation": "none"}, "vm": backend}}
    with pytest.raises(ValueError, match=why) as info:
        load(tmp_path, doc)
    assert "vm" in str(info.value), "原因里有后端名"


def test_firecracker_registers_disabled_with_capabilities(tmp_path: Path) -> None:
    assert "firecracker" in KINDS and set(CAPABILITIES) == {"snapshot", "block_attach", "vsock"}
    boxes = load(tmp_path, {"default": "local", "backends": {"local": {"kind": "local", "isolation": "none"}, "firecracker": FIRECRACKER_OFF}})
    assert boxes.names == ("firecracker", "local")
    assert boxes.enabled_names == ("local",)
    assert not boxes.enabled("firecracker") and boxes.enabled("local")
    assert boxes.capabilities("firecracker") == frozenset({"snapshot", "block_attach", "vsock"})
    assert boxes.capabilities("local") == frozenset()
    assert boxes.has_capability("firecracker", "snapshot") and not boxes.has_capability("local", "snapshot")


# --- 2：按租户策略选启用的后端 ---


SPECS: dict[str, dict[str, Any]] = {
    "gvisor": {"kind": "local", "isolation": "container"},
    "firecracker": {"kind": "local", "isolation": "microvm"},
}


async def test_policy_backend_or_default(tmp_path: Path) -> None:
    tenant = TenantContext(tenant_id="t1", user_id="u1")
    chosen = broker(tmp_path / "a", SPECS, "firecracker", {"sandbox": {"backend": "gvisor"}})
    lease = await chosen.acquire(tenant, SandboxSpec())
    assert lease.backend.name == "gvisor"
    await chosen.release(lease)
    default = broker(tmp_path / "b", SPECS, "firecracker")
    lease = await default.acquire(tenant, SandboxSpec())
    assert lease.backend.name == "firecracker"
    await default.release(lease)


# --- 3：停用（排空）---


@pytest.fixture(scope="module")
def bridge():
    try:
        b = cloudflaretest.FakeBridge()
    except cloudflaretest.MissingTool as err:
        pytest.fail(str(err), pytrace=False)
    yield b
    b.stop()


async def test_disabled_backend_drains(tmp_path: Path, bridge: cloudflaretest.FakeBridge) -> None:
    secrets = tmp_path / "secrets.json"
    LocalFileSecretStoreProvider(secrets).seed_secret(None, "sandbox/cloudflare-bridge-token", bridge.token)
    cf = {"kind": "cloudflare", "isolation": "microvm", "bridge_url": bridge.url}
    local = {"kind": "local", "isolation": "none"}
    policy = {"sandbox": {"backend": "cf"}}
    clock = Clock()
    before = broker(tmp_path / "w1", {"cf": cf, "local": local}, "local", policy, secrets=secrets, relational=tmp_path / "relational", clock=clock)
    tenant = TenantContext(tenant_id="t1", user_id="u1", session_id=S1)
    lease = await before.acquire(tenant, SandboxSpec(session_id=S1), reuse=True)
    await before.release(lease, keep=True)

    # 滚动重启后的 Worker：登记里 cf 停用了。
    after = broker(tmp_path / "w2", {"cf": {**cf, "enabled": False}, "local": local}, "local", policy,
                   secrets=secrets, relational=tmp_path / "relational", clock=clock)
    with pytest.raises(DeniedError) as info:
        await after.acquire(TenantContext(tenant_id="t1", user_id="u1", session_id=S2), SandboxSpec(session_id=S2), reuse=True)
    assert info.value.code == "sandbox_backend_disabled"
    assert "cf" in info.value.message and "登记" in info.value.message

    again = await after.acquire(tenant, SandboxSpec(session_id=S1), reuse=True)
    assert again.reused and again.env_id == lease.env_id, "已有的环境照常复用"
    await again.box.write_file(again.env_id, "drain.txt", b"still here")
    assert await again.box.read_file(again.env_id, "drain.txt") == b"still here", "已有的环境照常可用"
    await after.release(again, keep=True)

    report = await reconcile_once(after, grace=timedelta(minutes=10))
    assert report.orphans_destroyed == 0 and report.lost == 0 and report.reaped == 0, report
    [record] = await after.ledger.active("t1")  # type: ignore[union-attr]
    assert record.backend == "cf" and record.state == "ready", "对账不删排空中的环境"

    clock.now += timedelta(hours=1)
    report = await reconcile_once(after, grace=timedelta(minutes=10))
    assert report.reaped == 1, "空闲到期照常回收（销毁正常）"
    assert await after.ledger.active("t1") == []  # type: ignore[union-attr]


# --- 4：回退规则不动；停用的回退后端跳过 ---


async def fail_next_create(b: SandboxBroker, backend: str, category: ErrorCategory) -> None:
    (await b.sandboxes.backend(backend)).provider._faults.fail_next("create", category)  # type: ignore[attr-defined]


async def test_firecracker_never_degrades_without_min_isolation(tmp_path: Path) -> None:
    tenant = TenantContext(tenant_id="t1", user_id="u1")
    strict = broker(tmp_path / "a", SPECS, "gvisor", {"sandbox": {"backend": "firecracker", "fallback": ["gvisor"]}})
    await fail_next_create(strict, "firecracker", ErrorCategory.TRANSIENT)
    with pytest.raises(TransientError) as info:
        await strict.acquire(tenant, SandboxSpec())
    assert "gvisor" in info.value.message, "说明更弱的 gvisor 不用"

    relaxed = broker(tmp_path / "b", SPECS, "gvisor", {"sandbox": {"backend": "firecracker", "fallback": ["gvisor"], "min_isolation": "container"}})
    await fail_next_create(relaxed, "firecracker", ErrorCategory.TRANSIENT)
    lease = await relaxed.acquire(tenant, SandboxSpec())
    assert lease.backend.name == "gvisor"
    assert lease.record is not None and lease.record.fallback_from == "firecracker" and lease.fallback_reason.startswith("firecracker：")


async def test_disabled_fallback_backend_is_skipped(tmp_path: Path) -> None:
    tenant = TenantContext(tenant_id="t1", user_id="u1")
    specs = {**SPECS, "kata": {"kind": "local", "isolation": "microvm", "enabled": False}}
    b = broker(tmp_path, specs, "gvisor", {"sandbox": {"backend": "firecracker", "fallback": ["kata"]}})
    await fail_next_create(b, "firecracker", ErrorCategory.TRANSIENT)
    with pytest.raises(TransientError) as info:
        await b.acquire(tenant, SandboxSpec())
    assert "kata" in info.value.message and "停用" in info.value.message
