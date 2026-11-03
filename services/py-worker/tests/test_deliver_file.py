"""F3 验收（docs/plan/n18-file-delivery-f3.md 第 5 节第 1 条）：Worker 的 deliver_file 工具（agentruntime_worker.tools.deliver）。

- 登记成 write_reversible、要幂等键；读工作区里的文件，经 ObjectStore 存到 deliveries/<task>/…，元数据写进业务库的 task_artifacts（迁移 207）；
- 路径：拒绝绝对路径、..、符号链接逃逸、空段；上限（单文件、每个 Task 的个数与总字节数）只能由部署收窄；超限时不留下对象与记录；
- 同一个幂等键只写一次，Worker 重启后（换一个 ToolDriver）也一样；输出与日志里没有对象地址与文件内容；
- 沙箱模式经读取接口取文件，不碰本机目录。
实现还没有时整个文件跳过。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from agentruntime.common.v1.tenant_pb2 import TenantContext
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.enums import Effect
from agentruntime_core.errors import InvalidError
from agentruntime_core.testing import FakeClock

deliver = pytest.importorskip("agentruntime_worker.tools.deliver", reason="F3 的实现还没写：先审验收测试（docs/plan/n18-file-delivery-f3.md）")

TENANT = "t1"
WORKSPACE = "w1"
TASK = "tsk_01J0000000000000000000000T"
ART_ID = r"^art_[0-7][0-9A-HJKMNP-TV-Z]{25}$"


class Env:
    def __init__(self, tmp_path: Path, limits: Any = None) -> None:
        self.root = tmp_path / "workspaces"
        self.dir = self.root / TENANT / WORKSPACE
        self.dir.mkdir(parents=True)
        self.clock = FakeClock()
        self.objects = LocalDiskObjectStoreProvider(tmp_path / "objects", signing_key=b"deliver-file-test", clock=self.clock)
        self.store = SQLiteRelationalStoreProvider(tmp_path / "db")
        self.limits = limits or deliver.HARD_LIMITS
        self.tools = self.new_driver()

    def new_driver(self) -> LocalToolDriverProvider:
        provider = LocalToolDriverProvider()
        deliver.register_delivery_tools(provider, objects=self.objects, store=self.store, reader=deliver.DirectoryReader(self.root), limits=self.limits, clock=self.clock)
        return provider

    def write(self, rel: str, data: bytes) -> Path:
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    async def call(self, args: dict[str, Any], key: str = "k1", driver: LocalToolDriverProvider | None = None) -> Any:
        scope = TenantContext(tenant_id=TENANT, workspace_id=WORKSPACE)
        return await (driver or self.tools).bind(scope).invoke("deliver_file", "v1", args, idempotency_key=key, task_id=TASK)

    async def rows(self, tenant: str = TENANT) -> list[dict[str, Any]]:
        await self.store.migrate(deliver.MIGRATIONS)
        async with self.store.bind(TenantContext(tenant_id=tenant)).transaction(read_only=True) as tx:
            return [dict(r) for r in await tx.query("SELECT * FROM task_artifacts ORDER BY created_at")]

    async def objects_of(self, tenant: str = TENANT) -> list[str]:
        page = await self.objects.bind(TenantContext(tenant_id=tenant)).list(prefix="deliveries/")
        return [i.key for i in page.items]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


async def test_the_tool_is_registered_as_a_reversible_write(env: Env) -> None:
    spec = await env.tools.bind(TenantContext(tenant_id=TENANT)).describe("deliver_file", "v1")
    assert spec.effect == Effect.WRITE_REVERSIBLE
    assert spec.input_schema["required"] == ["path"]
    with pytest.raises(InvalidError):
        await env.tools.bind(TenantContext(tenant_id=TENANT, workspace_id=WORKSPACE)).invoke("deliver_file", "v1", {"path": "a.txt"}, task_id=TASK)


async def test_a_workspace_file_is_published_and_recorded(env: Env) -> None:
    data = b"quarter,rate\n3,0.12\n"
    env.write("out/report.csv", data)
    result = await env.call({"path": "out/report.csv", "name": "季度报告.csv"})
    out = result.output
    assert out["name"] == "季度报告.csv" and out["path"] == "out/report.csv" and out["media_type"] == "text/csv"
    assert out["size_bytes"] == len(data) and out["sha256"] == hashlib.sha256(data).hexdigest()
    import re

    assert re.match(ART_ID, out["artifact_id"])
    [row] = await env.rows()
    assert row["artifact_id"] == out["artifact_id"] and row["task_id"] == TASK and row["sha256"] == out["sha256"]
    assert row["expires_at"] > row["created_at"]
    assert deliver.RETENTION == timedelta(days=7)
    [key] = await env.objects_of()
    assert key.startswith(f"deliveries/{TASK}/") and key.endswith("/季度报告.csv")
    assert (await env.objects.bind(TenantContext(tenant_id=TENANT)).get(key)).data == data
    text = json.dumps(out, ensure_ascii=False)
    assert "deliveries/" not in text and row["object_uri"] not in text, "输出里不带对象地址"


async def test_the_display_name_defaults_to_the_file_name(env: Env) -> None:
    env.write("charts/plot.png", b"\x89PNG....")
    out = (await env.call({"path": "charts/plot.png"})).output
    assert out["name"] == "plot.png" and out["media_type"] == "image/png"


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.txt", "out/../../outside.txt", "out//x.txt", "", "out/", ".git/config"])
async def test_bad_paths_are_rejected_without_leaving_anything(env: Env, path: str) -> None:
    (env.root / TENANT / "outside.txt").write_text("not yours", "utf-8")
    env.write("out/x.txt", b"x")
    env.write(".git/config", b"[core]")
    with pytest.raises(InvalidError):
        await env.call({"path": path})
    assert await env.rows() == [] and await env.objects_of() == []


async def test_a_symlink_that_escapes_the_workspace_is_rejected(env: Env, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", "utf-8")
    link = env.dir / "leak.txt"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("这台机器上建不了符号链接")
    with pytest.raises(InvalidError):
        await env.call({"path": "leak.txt"})
    assert await env.rows() == []


async def test_limits_are_enforced_and_leave_nothing_behind(tmp_path: Path) -> None:
    env = Env(tmp_path, limits=deliver.DeliveryLimits(max_file_bytes=10, max_files_per_task=2, max_task_bytes=15))
    env.write("big.txt", b"x" * 11)
    with pytest.raises(InvalidError):
        await env.call({"path": "big.txt"}, key="big")
    env.write("a.txt", b"a" * 8)
    env.write("b.txt", b"b" * 8)
    env.write("c.txt", b"c")
    await env.call({"path": "a.txt"}, key="a")
    with pytest.raises(InvalidError):
        await env.call({"path": "b.txt"}, key="b")  # 合计 16 > 15
    await env.call({"path": "c.txt"}, key="c")
    env.write("d.txt", b"d")
    with pytest.raises(InvalidError):
        await env.call({"path": "d.txt"}, key="d")  # 第 3 个 > 2
    assert sorted(r["path"] for r in await env.rows()) == ["a.txt", "c.txt"]
    assert len(await env.objects_of()) == 2


def test_deployment_limits_can_only_narrow() -> None:
    hard = deliver.HARD_LIMITS
    assert (hard.max_file_bytes, hard.max_files_per_task, hard.max_task_bytes) == (25 * 2**20, 20, 100 * 2**20)
    narrowed = deliver.DeliveryLimits.from_env({"AR_DELIVERY_MAX_FILE_BYTES": "1048576", "AR_DELIVERY_MAX_FILES": "5"})
    assert (narrowed.max_file_bytes, narrowed.max_files_per_task, narrowed.max_task_bytes) == (2**20, 5, hard.max_task_bytes)
    widened = deliver.DeliveryLimits.from_env({"AR_DELIVERY_MAX_FILE_BYTES": str(10**12), "AR_DELIVERY_MAX_TASK_BYTES": str(10**12)})
    assert widened == hard, "比硬上限大的配置按硬上限算"
    with pytest.raises(ValueError):
        deliver.DeliveryLimits.from_env({"AR_DELIVERY_MAX_FILES": "many"})


async def test_the_same_idempotency_key_writes_once_even_after_a_restart(env: Env) -> None:
    env.write("out/report.txt", b"report")
    first = await env.call({"path": "out/report.txt"}, key="same")
    again = await env.call({"path": "out/report.txt"}, key="same", driver=env.new_driver())  # 新的 ToolDriver：进程内的幂等记录没了
    assert again.output["artifact_id"] == first.output["artifact_id"]
    assert len(await env.rows()) == 1 and len(await env.objects_of()) == 1


async def test_rows_belong_to_the_calling_tenant(env: Env) -> None:
    env.write("out/report.txt", b"report")
    await env.call({"path": "out/report.txt"})
    assert len(await env.rows(TENANT)) == 1
    assert await env.rows("t2") == [], "别的租户看不到这一行"
    assert await env.objects_of("t2") == []


async def test_file_content_stays_out_of_logs(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    marker = "CONTENT-MARKER-7f1c"
    env.write("out/notes.txt", marker.encode())
    with caplog.at_level(logging.DEBUG):
        await env.call({"path": "out/notes.txt"})
    assert marker not in caplog.text


class FakeSandboxReader:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.calls: list[tuple[str, str, str]] = []

    async def read(self, tenant_id: str, workspace_id: str, path: str, *, max_bytes: int) -> bytes:
        self.calls.append((tenant_id, workspace_id, path))
        data = self.files[path]
        if len(data) > max_bytes:
            raise InvalidError("delivery_file_too_large", "文件超过上限")
        return data


async def test_sandbox_mode_reads_through_the_reader(tmp_path: Path) -> None:
    env = Env(tmp_path)
    reader = FakeSandboxReader({"out/result.json": b'{"ok": true}'})
    provider = LocalToolDriverProvider()
    deliver.register_delivery_tools(provider, objects=env.objects, store=env.store, reader=reader, limits=env.limits, clock=env.clock)
    out = (await env.call({"path": "out/result.json"}, driver=provider)).output
    assert reader.calls == [(TENANT, WORKSPACE, "out/result.json")]
    assert out["media_type"] == "application/json" and out["size_bytes"] == len(b'{"ok": true}')
    assert not (env.dir / "out").exists(), "沙箱模式不碰本机的工作区目录"
