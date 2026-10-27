"""D-13：会话级沙箱（docs/plan/cloud-deploy.md D-13 的验收；设计书改动稿 W5）。

1. 同一会话的两条命令落在同一个环境（第二条的 span 标着复用、环境号相同，第一条起的后台进程第二条看得到）；不同会话各用各的；
   没有会话、或 AR_SANDBOX_SESSIONS=0 时每条命令一个环境。
2. 空闲到期后由对账回收；再跑命令换了新环境，工作区里的文件还在。
3. 换一个 Worker 副本（另一个 broker 实例，只共用业务库与后端）照样复用同一个环境，不靠进程内的缓存。
4. 一个工作区同一时间只有一个可写的环境：别的会话的那个闲着就回收它，正在跑命令就拒绝（workspace_busy）。
5. 复用的环境在后端里没了（被回收）：记为丢失，换新环境把这条命令跑完，调用方看不出来。
（验收 4 的“20 条命令在 Kata 上的总耗时”要 Kata 节点，没在本机测。）
"""

from __future__ import annotations

import io
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.adapters import cloudflaretest
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.errors import DeniedError
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.config import ENV_SANDBOX_SESSIONS, WorkerConfig
from agentruntime_worker.sandbox.broker import SandboxBroker
from agentruntime_worker.sandbox.ledger import SandboxLedger
from agentruntime_worker.sandbox.reconcile import reconcile_once
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.tools.sandboxes import Sandboxes

S1 = "ses_01HZY2J5K3M4N5P6Q7R8S9T0VW"
S2 = "ses_01HZY2J5K3M4N5P6Q7R8S9T0VX"


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


class Setup:
    def __init__(self, tmp_path: Path, *, sessions: bool = True, specs: Mapping[str, Any] | None = None, default: str = "local",
                 secrets: Path | None = None, relational: Path | None = None) -> None:
        self.clock = Clock()
        self.workspaces = tmp_path / "workspaces"
        (self.workspaces / "t1" / "w1").mkdir(parents=True, exist_ok=True)
        self.boxes = Sandboxes(dict(specs or {"local": {"kind": "local", "isolation": "none"}}), default, workspace_root=self.workspaces, secrets_file=secrets)
        self.ledger = SandboxLedger(SQLiteRelationalStoreProvider(relational or tmp_path / "relational"), clock=self.clock)
        self.broker = SandboxBroker(self.boxes, self.ledger)
        self.telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
        self.tools = LocalToolDriverProvider()
        register_platform_tools(
            self.tools, PlatformToolsConfig(workspace_root=self.workspaces, shell_sandbox="sandbox", session_sandboxes=sessions),
            sandboxes=self.broker, telemetry=self.telemetry,
        )

    async def shell(self, argv: list[str], session: str = S1, workspace: str = "w1", **extra: Any) -> dict[str, Any]:
        (self.workspaces / "t1" / workspace).mkdir(parents=True, exist_ok=True)
        tenant = TenantContext(tenant_id="t1", user_id="u1", workspace_id=workspace, session_id=session)
        out = (await self.tools.bind(tenant).invoke("shell_run", "v1", {"argv": argv, **extra}, idempotency_key=f"idk-{os.urandom(4).hex()}")).output
        await self.telemetry.bind(tenant).flush()
        return out

    def spans(self) -> list[dict[str, Any]]:
        return [dict(s.attributes) for s in self.telemetry.exported_spans if s.name == "runtime.sandbox"]


PY = ["python", "-c"]


async def test_same_session_reuses_one_environment_and_sessions_are_separate(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    await s.shell([*PY, "print(1)"])
    await s.shell([*PY, "print(2)"])
    await s.shell([*PY, "print(3)"], session=S2, workspace="w2")  # 同一个工作区只有一个可写环境，另一个会话用别的工作区
    first, second, other = s.spans()
    assert first["sandbox.reused"] is False and second["sandbox.reused"] is True
    assert first["sandbox.env_ref"] == second["sandbox.env_ref"] != other["sandbox.env_ref"]
    assert "sandbox.lifetime_ms" not in second, "会话级的环境不按一条命令算存活时间"
    active = await s.ledger.active("t1")
    assert {r.session_id for r in active} == {S1, S2} and all(r.state == "ready" for r in active)


async def test_background_process_from_the_first_command_is_seen_by_the_second(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    # 后台进程每 0.1 秒把计数写进 beat.txt；第二条命令隔一秒读两次，变了就是还活着。
    beat = "import time\nn = 0\nwhile n < 300:\n    n += 1\n    open('beat.txt', 'w').write(str(n))\n    time.sleep(0.1)\n"
    start = (
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-c', {beat!r}], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    )
    await s.shell([*PY, start])
    check = "import time\ntime.sleep(0.5)\na = open('beat.txt').read()\ntime.sleep(1)\nprint('alive' if open('beat.txt').read() != a else 'gone')\n"
    out = await s.shell([*PY, check])
    assert "alive" in out["stdout"], out
    assert s.spans()[-1]["sandbox.reused"] is True


async def test_without_a_session_or_with_sessions_off_every_command_is_one_shot(tmp_path: Path) -> None:
    s = Setup(tmp_path / "off", sessions=False)
    await s.shell([*PY, "print(1)"])
    await s.shell([*PY, "print(2)"])
    first, second = s.spans()
    assert first["sandbox.env_ref"] != second["sandbox.env_ref"] and second["sandbox.reused"] is False
    assert await s.ledger.active("t1") == [], "用完就删"
    s2 = Setup(tmp_path / "nosession")
    await s2.shell([*PY, "print(1)"], session="")
    assert await s2.ledger.active("t1") == []
    assert WorkerConfig.from_env({ENV_SANDBOX_SESSIONS: "0"}).sandbox_sessions is False
    assert WorkerConfig.from_env({}).sandbox_sessions is True
    with pytest.raises(ValueError):
        WorkerConfig.from_env({ENV_SANDBOX_SESSIONS: "yes"})


async def test_after_idle_expiry_a_new_environment_still_sees_the_workspace(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    await s.shell([*PY, "open('kept.txt', 'w').write('from the first environment')"])
    first = s.spans()[-1]["sandbox.env_ref"]
    s.clock.now += timedelta(minutes=16)
    report = await reconcile_once(s.broker, grace=timedelta(minutes=10))
    assert report.reaped == 1
    out = await s.shell([*PY, "print(open('kept.txt').read())"])
    assert "from the first environment" in out["stdout"]
    assert s.spans()[-1]["sandbox.env_ref"] != first and s.spans()[-1]["sandbox.reused"] is False


@pytest.fixture(scope="module")
def bridge():
    try:
        b = cloudflaretest.FakeBridge()
    except cloudflaretest.MissingTool as err:
        pytest.fail(str(err), pytrace=False)
    yield b
    b.stop()


async def test_another_worker_replica_reuses_the_same_environment(tmp_path: Path, bridge: cloudflaretest.FakeBridge) -> None:
    secrets = tmp_path / "secrets.json"
    LocalFileSecretStoreProvider(secrets).seed_secret(None, "sandbox/cloudflare-bridge-token", bridge.token)
    specs = {"cf": {"kind": "cloudflare", "isolation": "microvm", "bridge_url": bridge.url}}
    first = Setup(tmp_path / "w1", specs=specs, default="cf", secrets=secrets, relational=tmp_path / "relational")
    second = Setup(tmp_path / "w2", specs=specs, default="cf", secrets=secrets, relational=tmp_path / "relational")
    tenant = TenantContext(tenant_id="t1", user_id="u1", session_id=S1)
    lease = await first.broker.acquire(tenant, SandboxSpec(session_id=S1), reuse=True)
    await lease.box.write_file(lease.env_id, "state.txt", b"replica-1")
    await first.broker.release(lease, keep=True)
    again = await second.broker.acquire(tenant, SandboxSpec(session_id=S1), reuse=True)
    assert again.reused and again.env_id == lease.env_id, "另一个副本按环境表找到同一个环境"
    assert await again.box.read_file(again.env_id, "state.txt") == b"replica-1"
    await second.broker.release(again)


async def test_one_writable_environment_per_workspace(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    a = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1", session_id=S1)
    b = TenantContext(tenant_id="t1", user_id="u2", workspace_id="w1", session_id=S2)
    spec = SandboxSpec(workspace_id="w1")
    busy = await s.broker.acquire(a, SandboxSpec(workspace_id="w1", session_id=S1), reuse=True, busy_for=timedelta(minutes=5))
    with pytest.raises(DeniedError) as info:
        await s.broker.acquire(b, SandboxSpec(workspace_id="w1", session_id=S2), reuse=True)
    assert info.value.code == "workspace_busy", "那边正在跑命令"
    await s.broker.release(busy, keep=True)  # 命令跑完
    taken = await s.broker.acquire(b, SandboxSpec(workspace_id="w1", session_id=S2), reuse=True)
    assert taken.record is not None and taken.record.session_id == S2
    old = await s.ledger.get("t1", busy.record.env_ref)  # type: ignore[union-attr]
    assert old is not None and old.state == "destroyed" and "另一个会话" in old.reason, "闲着的那个被回收"
    one_shot = await s.broker.acquire(a, spec)
    assert one_shot.record is not None, "一次性的环境（没有会话）不占工作区"


async def test_reused_environment_gone_from_the_backend_is_replaced_transparently(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    await s.shell([*PY, "print(1)"])
    record = (await s.ledger.active("t1"))[0]
    local = (await s.boxes.backend("local")).provider
    await local.bind(TenantContext(tenant_id="t1", workspace_id="w1")).destroy(record.backend_env_id)  # 后端把它回收了
    out = await s.shell([*PY, "print('still works')"])
    assert out["exit_code"] == 0 and "still works" in out["stdout"]
    assert (await s.ledger.get("t1", record.env_ref)).state == "lost"  # type: ignore[union-attr]
    assert s.spans()[-1]["sandbox.env_ref"] != record.env_ref
