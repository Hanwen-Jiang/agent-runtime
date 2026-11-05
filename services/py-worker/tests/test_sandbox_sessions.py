"""D-13：会话级沙箱（docs/plan/cloud-deploy.md D-13 的验收；设计书改动稿 W5）。SBX-01 之后借还在 Sandbox Service 里，这里对着
真的服务进程（service_setup）看 Worker 一侧：

1. 同一会话的两条命令落在同一个环境（第二条的 span 标着复用、环境号相同，第一条起的后台进程第二条看得到）；不同会话各用各的；
   没有会话、或 AR_SANDBOX_SESSIONS=0 时每条命令一个环境。
2. 会话的环境没了（关会话、空闲到期被回收）之后再跑命令换了新环境，工作区里的文件还在。
3. 换一个 Worker 副本（另一个 ServiceBroker，只共用服务）照样复用同一个环境，不靠进程内的缓存。
4. 一个工作区同一时间只有一个可写的环境：别的会话的那个闲着就回收它，正在跑命令就拒绝（workspace_busy）。
5. 复用的环境在后端里没了（被回收）：记为丢失，换新环境把这条命令跑完，调用方看不出来。
（空闲到期的回收、对账与按 Cloudflare 后端复用由 services/go-sandbox-service 的测试覆盖；验收 4 的“20 条命令在 Kata 上的总耗时”
要 Kata 节点，没在本机测。）
"""

from __future__ import annotations

import json
import os
import signal
from datetime import timedelta
from pathlib import Path

import pytest

from agentruntime_core.errors import DeniedError
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.config import ENV_SANDBOX_SESSIONS, WorkerConfig
from agentruntime_worker.sandbox.client import ServiceBroker

from .sandbox_service_support import WORKER_TOKEN
from .service_setup import PY, S1, S2, Setup

__all__ = ["PY", "S1", "S2", "Setup"]


async def test_same_session_reuses_one_environment_and_sessions_are_separate(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    cold = await s.shell([*PY, "print(1)"])
    warm = await s.shell([*PY, "print(2)"])
    await s.shell([*PY, "print(3)"], session=S2, workspace="w2")  # 同一个工作区只有一个可写环境，另一个会话用别的工作区
    first, second, other = s.spans()
    assert first["sandbox.reused"] is False and second["sandbox.reused"] is True
    # 新建环境的那一次在结果里报冷启动时间（进度事件 tool_status 带给用户）；复用的没有。
    assert isinstance(cold["sandbox_startup_ms"], int) and cold["sandbox_startup_ms"] >= 0 and "sandbox_startup_ms" not in warm
    assert first["sandbox.env_ref"] == second["sandbox.env_ref"] != other["sandbox.env_ref"]
    assert first["sandbox.service"] is True, "经 Sandbox Service"
    assert "sandbox.lifetime_ms" not in second, "会话级的环境不按一条命令算存活时间"
    active = s.envs("t1")
    assert {r["session_id"] for r in active} == {S1, S2} and all(r["state"] == "ready" for r in active)


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
    assert s.envs("t1") == [], "用完就删"
    s2 = Setup(tmp_path / "nosession")
    await s2.shell([*PY, "print(1)"], session="")
    assert s2.envs("t1") == []
    assert WorkerConfig.from_env({ENV_SANDBOX_SESSIONS: "0"}).sandbox_sessions is False
    assert WorkerConfig.from_env({}).sandbox_sessions is True
    with pytest.raises(ValueError):
        WorkerConfig.from_env({ENV_SANDBOX_SESSIONS: "yes"})


async def test_after_the_session_environment_is_gone_a_new_one_still_sees_the_workspace(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    await s.shell([*PY, "open('kept.txt', 'w').write('from the first environment')"])
    first = s.spans()[-1]["sandbox.env_ref"]
    assert await s.broker.destroy_session("t1", workspace_id="w1", session_id=S1) == 1
    out = await s.shell([*PY, "print(open('kept.txt').read())"])
    assert "from the first environment" in out["stdout"]
    assert s.spans()[-1]["sandbox.env_ref"] != first and s.spans()[-1]["sandbox.reused"] is False


async def test_another_worker_replica_reuses_the_same_environment(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    second = ServiceBroker.connect(s.svc.address, WORKER_TOKEN)
    tenant = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1", session_id=S1)
    lease = await s.broker.acquire(tenant, SandboxSpec(workspace_id="w1"), reuse=True)
    await lease.box.write_file(lease.env_id, "state.txt", b"replica-1")
    await s.broker.release(lease, keep=True)
    again = await second.acquire(tenant, SandboxSpec(workspace_id="w1"), reuse=True)
    assert again.reused and again.env_id == lease.env_id, "另一个副本经服务按环境表找到同一个环境"
    assert await again.box.read_file(again.env_id, "state.txt") == b"replica-1"
    await second.release(again)
    await second.aclose()


async def test_one_writable_environment_per_workspace(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    a = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1", session_id=S1)
    b = TenantContext(tenant_id="t1", user_id="u2", workspace_id="w1", session_id=S2)
    busy = await s.broker.acquire(a, SandboxSpec(workspace_id="w1"), reuse=True, busy_for=timedelta(minutes=5))
    with pytest.raises(DeniedError) as info:
        await s.broker.acquire(b, SandboxSpec(workspace_id="w1"), reuse=True)
    assert info.value.code == "workspace_busy", "那边正在跑命令"
    await s.broker.release(busy, keep=True)  # 命令跑完
    taken = await s.broker.acquire(b, SandboxSpec(workspace_id="w1"), reuse=True)
    assert taken.record is not None and taken.record.session_id == S2
    old = next(r for r in s.envs("t1", active=False) if r["env_ref"] == busy.env_id)
    assert old["state"] == "destroyed" and "另一个会话" in old["reason"], "闲着的那个被回收"
    one_shot = await s.broker.acquire(TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1"), SandboxSpec(workspace_id="w1"))
    assert one_shot.record is not None, "一次性的环境（没有会话）不占工作区"
    await s.broker.release(one_shot)


async def test_reused_environment_gone_from_the_backend_is_replaced_transparently(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    await s.shell([*PY, "print(1)"])
    record = s.envs("t1")[0]
    meta = json.loads((s.svc.state / "local" / "t1" / record["backend_env_id"] / "meta.json").read_text("utf-8"))
    os.kill(meta["pid"], signal.SIGKILL)  # 后端把它回收了
    out = await s.shell([*PY, "print('still works')"])
    assert out["exit_code"] == 0 and "still works" in out["stdout"]
    assert out["sandbox_startup_ms"] >= 0, "换了新环境：报新环境的冷启动时间"
    gone = next(r for r in s.envs("t1", active=False) if r["env_ref"] == record["env_ref"])
    assert gone["state"] == "lost"
    assert s.spans()[-1]["sandbox.env_ref"] != record["env_ref"]
