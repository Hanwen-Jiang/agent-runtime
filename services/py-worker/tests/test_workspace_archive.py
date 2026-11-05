"""D-06c、D-06d：工作区的存档模式（docs/plan/workspace-layer.md 第 2～4 节、第 6 节的验收 1、2、3、6）。

登记里 workspace: archive 的后端不挂工作区卷：建环境后从最近的快照恢复，可写的环境销毁之前存档；repo_* 与 shell_run 在
承载它的环境里执行。存档与恢复在 Sandbox Service 里（SBX-01）；这里对着真的服务进程（service_setup，本地后端加上
workspace: archive），ObjectStore 是两边共用的本地目录；Worker 不配工作区目录（只登记存档模式的后端时也有工作区类工具）。

1. 会话级的环境被回收后，新环境从快照恢复，文件还在；宿主上没有工作区目录。
2. 一次性的命令（没有会话）：每条命令建环境、恢复、跑、存档、销毁，下一条看得到上一条写的。
3. repo_edit、repo_read、repo_list、repo_grep 在环境里执行，与 shell_run 看到同一份。
4. 存档按内容寻址：没变的文件不重写；只留最近 3 份快照，没人引用的块清掉；恢复后逐文件的 sha256 相同。
5. 快照里的块被改过：换一个环境再恢复一次，仍不一致报 invalid，不在半个工作区上执行，环境都销毁。
6. 两个租户同名的工作区互不可见（ObjectStore 与业务库都按租户分）。
7. 存档模式的后端之间可以回退（同一组），回退的环境也从快照恢复（要往后端注入故障，在 services/go-sandbox-service 的测试里）。
8. 别的会话要写同一个工作区：闲着的那个先存档再回收，新环境看得到它写的（一个工作区同一时间只有一个可写的环境）。
9. 工作区的个数与大小按租户策略：超了个数 denied；超了大小时这次的改动没存进快照，shell_run 的结果里写明。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.errors import DeniedError, InvalidError
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.workspace import blob_key

from . import service_setup
from .service_setup import ARCHIVE, S1, S2


class Setup(service_setup.Setup):
    def __init__(self, tmp_path: Path, *, sessions: bool = True, specs: Mapping[str, Any] | None = None, policy: Mapping[str, Any] | None = None) -> None:
        super().__init__(tmp_path, mode="archive", specs=specs, sessions=sessions, policies={t: policy for t in ("t1", "t2")} if policy else None)
        assert {"repo_read", "repo_edit", "shell_run"} <= set(self.names), "只有存档模式的后端时也登记工作区类工具"


async def test_reaped_session_environment_is_restored_from_the_snapshot(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    await s.py("import os; os.makedirs('src', exist_ok=True); open('src/kept.txt', 'w').write('from the first environment')")
    assert s.spans()[-1]["workspace.mode"] == "archive"
    assert await s.broker.destroy_session("t1", workspace_id="w1", session_id=S1) == 1  # 同空闲到期被回收：先存档再销毁
    snap = await s.archiver.latest("t1", "w1")
    assert snap is not None and snap.files == 1
    out = await s.py("print(open('src/kept.txt').read())")
    assert "from the first environment" in out["stdout"], out
    assert s.spans()[-1]["workspace.restored_snapshot"] == snap.snapshot_id
    assert not (s.host / "t1").exists(), "存档模式不碰宿主的工作区目录"


async def test_one_shot_commands_hand_the_workspace_over_through_snapshots(tmp_path: Path) -> None:
    s = Setup(tmp_path, sessions=False)
    await s.py("open('a.txt', 'w').write('1')")
    assert s.envs("t1") == [], "一次性的环境用完就删"
    out = await s.py("print(open('a.txt').read() + '2')")
    assert out["stdout"].strip() == "12"


async def test_repo_tools_run_inside_the_hosting_environment(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    body = "def refund():\n    return 'ok'\n"
    edit = await s.call("repo_edit", {"path": "pkg/refund.py", "new": body})
    assert edit["created"] is True and edit["sha256"] == hashlib.sha256(body.encode()).hexdigest()
    read = await s.call("repo_read", {"path": "pkg/refund.py"})
    assert read["content"] == body and read["total_lines"] == 2
    listed = await s.call("repo_list", {"path": "pkg"})
    assert listed["entries"] == [{"name": "refund.py", "type": "file", "size": len(body)}]
    found = await s.call("repo_grep", {"pattern": "return 'ok'"})
    assert found["matches"] == [{"path": "pkg/refund.py", "line": 2, "text": "    return 'ok'"}]
    replaced = await s.call("repo_edit", {"path": "pkg/refund.py", "old": "'ok'", "new": "'done'"})
    assert replaced["created"] is False
    out = await s.py("import pkg.refund as r; print(r.refund())")
    assert out["stdout"].strip() == "done", "工具与命令看到的是同一个环境"
    assert len({span["sandbox.env_ref"] for span in s.spans()}) == 1
    with pytest.raises(InvalidError):
        await s.call("repo_read", {"path": "../outside.txt"})
    with pytest.raises(Exception) as missing:
        await s.call("repo_read", {"path": "pkg/missing.py"})
    assert getattr(missing.value, "code", "") == "file_not_found"
    assert not (s.host / "t1").exists()


async def test_reads_in_a_new_session_see_the_last_snapshot(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    await s.call("repo_edit", {"path": "notes.md", "new": "v1\n"})
    await s.broker.destroy_session("t1", workspace_id="w1", session_id=S1)
    # 另一个会话只读：借的是在快照上建的只读环境，不占可写的位置。
    read = await s.call("repo_read", {"path": "notes.md"}, session=S2)
    assert read["content"] == "v1\n"
    assert [(r["session_id"], r["writable"]) for r in s.envs("t1")] == [(S2, 0)]


async def test_snapshots_are_content_addressed_and_old_ones_are_cleaned(tmp_path: Path) -> None:
    s = Setup(tmp_path, sessions=False)
    await s.py("open('big.txt', 'w').write('x' * 10000); open('n.txt', 'w').write('0')")
    first = await s.archiver.latest("t1", "w1")
    assert first is not None and first.new_blobs == 2
    for i in range(1, 5):
        await s.py(f"open('n.txt', 'w').write('{i}')")
        latest = await s.archiver.latest("t1", "w1")
        assert latest is not None and latest.new_blobs == 1, "没变的 big.txt 不再写"
    store = s.objects.bind(TenantContext(tenant_id="t1"))
    blobs = {i.key for i in (await store.list(prefix="workspaces/w1/blobs/")).items}
    assert len(blobs) == 1 + 3, "big.txt 一份，n.txt 只剩最近 3 份快照引用的"
    manifests = {i.key for i in (await store.list(prefix="workspaces/w1/snapshots/")).items}
    assert len(manifests) == 3 and not any(first.snapshot_id in k for k in manifests)
    # 恢复后逐文件的 sha256 与快照的清单相同。
    snap = await s.archiver.latest("t1", "w1")
    assert snap is not None
    want = {e.path: e.sha256 for e in await s.archiver.entries("t1", snap) if e.type == "file"}
    out = await s.py("import hashlib, json, os; print(json.dumps({p: hashlib.sha256(open(p, 'rb').read()).hexdigest() for p in sorted(os.listdir('.')) if os.path.isfile(p)}))")
    assert __import__("json").loads(out["stdout"]) == want


async def test_tampered_snapshot_is_not_restored(tmp_path: Path) -> None:
    s = Setup(tmp_path, sessions=False)
    await s.py("open('a.txt', 'w').write('original')")
    snap = await s.archiver.latest("t1", "w1")
    assert snap is not None
    [entry] = [e for e in await s.archiver.entries("t1", snap) if e.path == "a.txt"]
    await s.objects.bind(TenantContext(tenant_id="t1")).put(blob_key("w1", entry.sha256), b"tampered")
    with pytest.raises(InvalidError) as info:
        await s.py("print(open('a.txt').read())")
    assert info.value.code == "workspace_restore_mismatch"
    assert s.envs("t1") == [], "恢复失败的环境都销毁了"


async def test_same_workspace_id_is_separate_per_tenant(tmp_path: Path) -> None:
    s = Setup(tmp_path, sessions=False)
    await s.py("open('mine.txt', 'w').write('t1')")
    out = await s.py("import os; print(os.path.exists('mine.txt'))", tenant_id="t2")
    assert out["stdout"].strip() == "False"
    assert await s.archiver.latest("t2", "w1") is not None and (await s.archiver.latest("t2", "w1")) != (await s.archiver.latest("t1", "w1"))


async def test_another_session_takes_over_after_the_idle_one_is_archived(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    await s.py("open('handoff.txt', 'w').write('written by S1')")
    out = await s.py("print(open('handoff.txt').read())", session=S2)
    assert out["stdout"].strip() == "written by S1"
    assert [(r["session_id"], r["writable"]) for r in s.envs("t1")] == [(S2, 1)], "S1 的环境存档后被回收"


async def test_workspace_count_and_size_follow_the_tenant_policy(tmp_path: Path) -> None:
    s = Setup(tmp_path, sessions=False, policy={"workspace": {"max_workspaces": 1, "max_bytes": 100}})
    await s.py("print(1)")
    with pytest.raises(DeniedError) as info:
        await s.py("print(1)", workspace="w2")
    assert info.value.code == "workspace_quota_exceeded"
    out = await s.py("open('big.bin', 'w').write('x' * 1000)")
    assert out["workspace_error"]["code"] == "too_large", out
    assert s.envs("t1") == [], "超过大小上限的环境照样销毁"
