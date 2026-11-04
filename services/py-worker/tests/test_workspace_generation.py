"""WS-READ-03 验收（docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 2 节）：工作区的版本号（generation）。

task-service 按（租户、工作区、generation、操作与参数）缓存只读操作的结果；generation 一变，缓存就不再命中。这里核对 Worker 一侧：
1. 改动工作区的都让 generation 加一：Workspace API 的写操作（建、提交、丢弃改动、同步……），写类工具（repo_edit、shell_run），
   挂卷与存档两种模式都一样。
2. 只读的不动它：Workspace API 的列目录、读文件、提交记录、diff，读类工具（repo_read、repo_list、repo_grep）。
3. 业务库里还没有这个工作区的行时（直接用工具、没经 Workspace API 建）不报错，也不凭空建行。
"""

from __future__ import annotations

import io
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import tenant_pb2
from agentruntime.workflow.v1 import workspace_pb2
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.broker import SandboxBroker
from agentruntime_worker.sandbox.ledger import SandboxLedger
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.tools.sandboxes import Sandboxes
from agentruntime_worker.workspace import WorkspaceArchiver
from agentruntime_worker.workspace.operations import WorkspaceOperations

S1 = "ses_01HZY2J5K3M4N5P6Q7R8S9T0VW"
Op = workspace_pb2.WorkspaceOperation


def git(*args: str, cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", check=True).stdout


def make_remote(tmp_path: Path) -> str:
    bare, seed = tmp_path / "remote.git", tmp_path / "seed"
    git("init", "--quiet", "--bare", "-b", "main", str(bare))
    git("init", "--quiet", "-b", "main", str(seed))
    (seed / "README.md").write_bytes(b"# refund service\n")
    git("add", "-A", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--quiet", "-m", "seed", cwd=seed)
    git("push", "--quiet", str(bare), "main", cwd=seed)
    return bare.as_uri()


class Setup:
    def __init__(self, tmp_path: Path, *, mode: str) -> None:
        self.host = tmp_path / "workspaces"
        spec = {"kind": "local", "isolation": "none", "workspace": mode}

        async def lookup(tenant_id: str) -> Mapping[str, Any] | None:
            return None

        boxes = Sandboxes({"local": spec}, "local", workspace_root=self.host if mode == "mount" else None, policy=lookup)
        store = SQLiteRelationalStoreProvider(tmp_path / "relational")
        self.archiver = WorkspaceArchiver(store, LocalDiskObjectStoreProvider(tmp_path / "objects", signing_key=os.urandom(32)))
        broker = SandboxBroker(boxes, SandboxLedger(store), archiver=self.archiver)

        async def secret() -> bytes:
            return b"k" * 32

        self.ops = WorkspaceOperations(broker, secret=secret, allow_local_remotes=True)
        self.tools = LocalToolDriverProvider()
        register_platform_tools(
            self.tools, PlatformToolsConfig(workspace_root=self.host if mode == "mount" else None, shell_sandbox="sandbox", session_sandboxes=True),
            sandboxes=broker, telemetry=ConsoleTelemetryProvider(stream=io.StringIO()),
        )

    async def run(self, op: int, **args: Any) -> dict[str, Any]:
        msg = workspace_pb2.WorkspaceOperationInput(
            tenant=tenant_pb2.TenantContext(tenant_id="t1", user_id="u-alice"), operation_id="wso-" + os.urandom(13).hex(), operation=op,
            workspace_id="w1", requested_by="u-alice",
        )
        if args:
            msg.args.CopyFrom(json_format.ParseDict(args, struct_pb2.Struct()))
        out = await self.ops.run(msg)
        assert not out.HasField("error"), out.error
        return json_format.MessageToDict(out.result)

    async def tool(self, name: str, args: dict[str, Any], workspace: str = "w1") -> dict[str, Any]:
        tenant = TenantContext(tenant_id="t1", user_id="u1", workspace_id=workspace, session_id=S1)
        return (await self.tools.bind(tenant).invoke(name, "v1", args, idempotency_key=f"idk-{os.urandom(4).hex()}")).output

    async def generation(self, workspace: str = "w1") -> int | None:
        return await self.archiver.generation("t1", workspace)


@pytest.mark.parametrize("mode", ["archive", "mount"])
async def test_writes_bump_the_generation_and_reads_do_not(tmp_path: Path, mode: str) -> None:
    s = Setup(tmp_path, mode=mode)
    await s.run(Op.WORKSPACE_OPERATION_CREATE, repo_url=make_remote(tmp_path))
    created = await s.generation()
    assert created is not None and created >= 1, "建也是一次改动"

    await s.run(Op.WORKSPACE_OPERATION_LIST_FILES, path="")
    await s.run(Op.WORKSPACE_OPERATION_READ_FILE, path="README.md")
    await s.run(Op.WORKSPACE_OPERATION_LOG)
    await s.run(Op.WORKSPACE_OPERATION_DIFF)
    await s.tool("repo_read", {"path": "README.md"})
    await s.tool("repo_list", {})
    await s.tool("repo_grep", {"pattern": "refund"})
    assert await s.generation() == created, "只读的不动 generation"

    await s.tool("repo_edit", {"path": "app.py", "new": "x = 1\n"})
    assert await s.generation() == created + 1
    await s.tool("shell_run", {"argv": ["python", "-c", "open('n.txt', 'w').write('1')"]})
    assert await s.generation() == created + 2
    await s.run(Op.WORKSPACE_OPERATION_COMMIT, message="change")
    assert await s.generation() == created + 3
    await s.tool("repo_edit", {"path": "app.py", "new": "x = 2\n"})
    await s.run(Op.WORKSPACE_OPERATION_DISCARD)
    assert await s.generation() == created + 5


async def test_a_workspace_without_a_row_is_left_alone(tmp_path: Path) -> None:
    s = Setup(tmp_path, mode="mount")
    out = await s.tool("repo_edit", {"path": "a.txt", "new": "x"}, workspace="loose")
    assert out["created"]
    assert await s.generation("loose") is None, "工具不凭空建工作区的行"
