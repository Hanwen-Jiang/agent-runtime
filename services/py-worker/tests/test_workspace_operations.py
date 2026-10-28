"""D-06e：Workspace API 在 Worker 一侧的操作（docs/plan/workspace-layer.md 第 5 节、第 6 节的验收 3、4、5）。

远端是本机的裸仓库（file://，只在 allow_local_remotes 时收；生产只收 https）。存档模式与挂卷两种后端各跑一遍主流程。

1. 建（克隆）：工作区的 head 是远端的 HEAD；工具（repo_read）看到克隆下来的文件；同名再建 conflict；没建成时撤掉这一行。
2. diff：会话里改的、新加的文件都在；内容与沙箱里 git diff 的输出一致；存档模式下会话的环境闲着时先存档、不回收它。
3. 提交：有改动时 committed，再提交一次没有改动时 committed 为 false、sha 不变。
4. 推送：分支不在策略里 denied；推到 agent/* 成功，远端有这个 sha；同一个提交再推一次 reconciled；gate 记录的批准人是发起的用户。
5. 凭据：封给别的租户或别的操作的凭据解不开（denied）；错误信息里没有凭据。
6. 别的租户同名的工作区 not_found；存档、删除（删除后快照与环境都没了，挂卷时目录也删了）。
7. 经 Temporal 跑工作流：结果原样带回；推送遇到暂时性的错误不重试，写成 transient 的结果。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from google.protobuf import json_format, struct_pb2
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentruntime.common.v1 import error_pb2, tenant_pb2
from agentruntime.workflow.v1 import workspace_pb2
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.sealing import seal
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker import names
from agentruntime_worker.sandbox.broker import SandboxBroker
from agentruntime_worker.sandbox.ledger import SandboxLedger
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.tools.sandboxes import Sandboxes
from agentruntime_worker.workflows.workspace import WorkspaceOperationWorkflow, operation_workflow_id
from agentruntime_worker.workspace import WorkspaceArchiver
from agentruntime_worker.workspace.operations import WorkspaceActivities, WorkspaceOperations, branch_allowed, check_branch, push_key

S1 = "ses_01HZY2J5K3M4N5P6Q7R8S9T0VW"
KEY = b"k" * 32
SECRET = "pw-SECRET-1234567890"
Op = workspace_pb2.WorkspaceOperation


def git(*args: str, cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    out = subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", check=True)
    return out.stdout.strip()


def make_remote(tmp_path: Path) -> tuple[str, Path]:
    bare = tmp_path / "remote.git"
    git("init", "--quiet", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git("init", "--quiet", "-b", "main", str(seed))
    (seed / "README.md").write_bytes(b"# refund service\n")
    (seed / "app.py").write_bytes(b"def refund():\n    return 1\n")
    git("add", "-A", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--quiet", "-m", "seed", cwd=seed)
    git("push", "--quiet", str(bare), "main", cwd=seed)
    return bare.as_uri(), bare


class Setup:
    def __init__(self, tmp_path: Path, *, mode: str, policy: Mapping[str, Any] | None = None) -> None:
        self.host = tmp_path / "workspaces"
        spec = {"kind": "local", "isolation": "none", "workspace": mode}

        async def lookup(tenant_id: str) -> Mapping[str, Any] | None:
            return policy

        self.boxes = Sandboxes({"local": spec}, "local", workspace_root=self.host if mode == "mount" else None, policy=lookup)
        store = SQLiteRelationalStoreProvider(tmp_path / "relational")
        self.ledger = SandboxLedger(store)
        self.archiver = WorkspaceArchiver(store, LocalDiskObjectStoreProvider(tmp_path / "objects", signing_key=os.urandom(32)))
        self.broker = SandboxBroker(self.boxes, self.ledger, archiver=self.archiver)

        async def secret() -> bytes:
            return KEY

        self.ops = WorkspaceOperations(self.broker, secret=secret, allow_local_remotes=True)
        self.tools = LocalToolDriverProvider()
        register_platform_tools(
            self.tools, PlatformToolsConfig(workspace_root=self.host if mode == "mount" else None, shell_sandbox="sandbox", session_sandboxes=True),
            sandboxes=self.broker, telemetry=ConsoleTelemetryProvider(stream=io.StringIO()),
        )

    @staticmethod
    def inp(op: int, workspace: str = "w1", tenant: str = "t1", sealed_for: tuple[str, str] | None = None, **args: Any) -> workspace_pb2.WorkspaceOperationInput:
        operation_id = "wso-" + uuid.uuid4().hex[:26]
        msg = workspace_pb2.WorkspaceOperationInput(
            tenant=tenant_pb2.TenantContext(tenant_id=tenant, user_id="u-alice"), operation_id=operation_id, operation=op, workspace_id=workspace,
            requested_by="u-alice",
        )
        if args:
            msg.args.CopyFrom(json_format.ParseDict(args, struct_pb2.Struct()))
        if sealed_for is not None:
            for_tenant, for_op = sealed_for
            msg.sealed_credential = seal(KEY, for_tenant, for_op or operation_id, json.dumps({"username": "bot", "password": SECRET}).encode())
        return msg

    async def run(self, op: int, **kw: Any) -> dict[str, Any]:
        out = await self.ops.run(self.inp(op, **kw))
        if out.HasField("error"):
            raise AssertionError(f"{out.error.code}: {out.error.message}")
        return json_format.MessageToDict(out.result)

    async def error(self, op: int, **kw: Any) -> error_pb2.Error:
        out = await self.ops.run(self.inp(op, **kw))
        assert out.HasField("error"), out
        return out.error

    async def tool(self, name: str, args: dict[str, Any], workspace: str = "w1") -> dict[str, Any]:
        tenant = TenantContext(tenant_id="t1", user_id="u1", workspace_id=workspace, session_id=S1)
        return (await self.tools.bind(tenant).invoke(name, "v1", args, idempotency_key=f"idk-{os.urandom(4).hex()}")).output


@pytest.mark.parametrize("mode", ["archive", "mount"])
async def test_clone_diff_commit_push(tmp_path: Path, mode: str) -> None:
    url, bare = make_remote(tmp_path)
    s = Setup(tmp_path, mode=mode)
    created = (await s.run(Op.WORKSPACE_OPERATION_CREATE, repo_url=url, sealed_for=("t1", "")))["workspace"]
    remote_head = git("rev-parse", "main", cwd=bare)
    assert created["workspace_id"] == "w1" and created["head"] == remote_head and created["repo_url"] == url
    assert (await s.tool("repo_read", {"path": "README.md"}))["content"] == "# refund service\n"
    conflict = await s.error(Op.WORKSPACE_OPERATION_CREATE, repo_url=url)
    assert conflict.category == error_pb2.ERROR_CATEGORY_CONFLICT

    # 会话里改一个已跟踪的文件：API 的 diff 与沙箱里 git diff 的输出一致。
    await s.tool("shell_run", {"argv": ["python", "-c", "open('app.py', 'ab').write(b'# changed\\n')"]})
    in_sandbox = (await s.tool("shell_run", {"argv": ["git", "-c", "safe.directory=*", "-c", "core.autocrlf=false", "diff", "--no-color", "HEAD"]}))["stdout"]
    diff = await s.run(Op.WORKSPACE_OPERATION_DIFF)
    assert diff["files"] == ["app.py"] and diff["diff"] == in_sandbox and "+# changed" in diff["diff"], diff
    assert diff["head"] == remote_head and diff["truncated"] is False
    if mode == "archive":
        writable = [(r.session_id, r.state) for r in await s.ledger.active("t1") if r.writable]
        assert writable == [(S1, "ready")], "会话的可写环境存档后还在（diff 在只读的环境里做）"

    # 新加的文件（未跟踪）也在 diff 里。
    await s.tool("repo_edit", {"path": "docs/new.md", "new": "new file\n"})
    diff = await s.run(Op.WORKSPACE_OPERATION_DIFF)
    assert diff["files"] == ["app.py", "docs/new.md"] and "new file mode" in diff["diff"]

    commit = await s.run(Op.WORKSPACE_OPERATION_COMMIT, message="修退款\n\n两处改动", author_name="Alice", author_email="alice@example.com")
    assert commit["committed"] is True and commit["sha"] != remote_head
    again = await s.run(Op.WORKSPACE_OPERATION_COMMIT, message="没有改动")
    assert again == {"sha": commit["sha"], "committed": False}
    assert (await s.run(Op.WORKSPACE_OPERATION_DIFF))["files"] == []

    denied = await s.error(Op.WORKSPACE_OPERATION_PUSH, branch="main")
    assert denied.category == error_pb2.ERROR_CATEGORY_DENIED and denied.code == "branch_not_allowed"
    pushed = await s.run(Op.WORKSPACE_OPERATION_PUSH, branch="agent/fix-refund", sealed_for=("t1", ""))
    assert pushed == {"sha": commit["sha"], "branch": "agent/fix-refund", "reconciled": False}
    assert git("rev-parse", "agent/fix-refund", cwd=bare) == commit["sha"]
    assert git("log", "-1", "--format=%an <%ae>|%s", "agent/fix-refund", cwd=bare) == "Alice <alice@example.com>|修退款"
    replay = await s.run(Op.WORKSPACE_OPERATION_PUSH, branch="agent/fix-refund")
    assert replay["reconciled"] is True, "同一个提交再推一次：先对账，不重推"
    [gate] = await s.ops.pushes("t1", "w1")
    assert gate["push_key"] == push_key("t1", "w1", "agent/fix-refund", commit["sha"]) and gate["approved_by"] == "u-alice"
    assert gate["state"] == "reconciled"


async def test_credentials_are_bound_to_the_tenant_and_operation_and_never_echoed(tmp_path: Path) -> None:
    url, _ = make_remote(tmp_path)
    s = Setup(tmp_path, mode="archive")
    moved = await s.error(Op.WORKSPACE_OPERATION_CREATE, repo_url=url, sealed_for=("t2", ""))
    assert moved.category == error_pb2.ERROR_CATEGORY_DENIED and moved.code == "invalid_sealed"
    other_op = await s.error(Op.WORKSPACE_OPERATION_CREATE, repo_url=url, sealed_for=("t1", "wso-someotheroperation0000000"))
    assert other_op.code == "invalid_sealed"
    missing = await s.error(Op.WORKSPACE_OPERATION_CREATE, repo_url=url + "-missing", sealed_for=("t1", ""))
    assert SECRET not in missing.message and "Basic" not in missing.message
    assert await s.archiver.get("t1", "w1") is not None and (await s.archiver.get("t1", "w1")).state == "deleted", "没建成：撤掉这一行"  # type: ignore[union-attr]
    assert (await s.run(Op.WORKSPACE_OPERATION_CREATE, repo_url=url))["workspace"]["workspace_id"] == "w1", "撤掉之后可以用同一个名字再建"
    for bad in ("https://user:pw@example.com/r.git", "http://example.com/r.git", "ssh://git@example.com/r.git"):
        err = await s.error(Op.WORKSPACE_OPERATION_CREATE, workspace="w9", repo_url=bad)
        assert err.category == error_pb2.ERROR_CATEGORY_INVALID and "pw" not in err.message


@pytest.mark.parametrize("mode", ["archive", "mount"])
async def test_other_tenants_archive_and_delete(tmp_path: Path, mode: str) -> None:
    s = Setup(tmp_path, mode=mode)
    await s.run(Op.WORKSPACE_OPERATION_CREATE)  # git init
    await s.tool("repo_edit", {"path": "a.txt", "new": "a\n"})
    other = await s.error(Op.WORKSPACE_OPERATION_DIFF, tenant="t2")
    assert other.category == error_pb2.ERROR_CATEGORY_NOT_FOUND
    archived = await s.run(Op.WORKSPACE_OPERATION_ARCHIVE)
    assert archived["snapshot_id"].startswith("wss-") and archived["files"] >= 1
    assert await s.run(Op.WORKSPACE_OPERATION_DELETE) == {}
    assert (await s.archiver.get("t1", "w1")).state == "deleted"  # type: ignore[union-attr]
    assert await s.archiver.latest("t1", "w1") is None
    assert await s.ledger.active("t1") == []
    assert not (s.host / "t1" / "w1").exists()
    gone = await s.error(Op.WORKSPACE_OPERATION_DIFF)
    assert gone.category == error_pb2.ERROR_CATEGORY_NOT_FOUND


async def test_workspace_count_follows_the_policy(tmp_path: Path) -> None:
    s = Setup(tmp_path, mode="archive", policy={"workspace": {"max_workspaces": 1, "push_branches": ["release/**"]}})
    await s.run(Op.WORKSPACE_OPERATION_CREATE)
    over = await s.error(Op.WORKSPACE_OPERATION_CREATE, workspace="w2")
    assert over.category == error_pb2.ERROR_CATEGORY_DENIED and over.code == "workspace_quota_exceeded"


def test_branch_rules() -> None:
    assert branch_allowed(("agent/*",), "agent/fix") and not branch_allowed(("agent/*",), "agent/a/b")
    assert branch_allowed(("release/**",), "release/2026/10") and not branch_allowed(("release/**",), "main")
    for bad in ("-x", "a..b", "a b", "a~1", "a^", "a/", "a.lock", "a//b", "@{x}", ".hidden"):
        with pytest.raises(Exception):
            check_branch(bad)


async def test_the_workflow_carries_results_and_does_not_retry_pushes(tmp_path: Path, temporal_env: WorkflowEnvironment) -> None:
    url, _ = make_remote(tmp_path)
    s = Setup(tmp_path, mode="archive")
    client: Client = temporal_env.client
    async with (
        Worker(client, task_queue=names.TASK_QUEUE, workflows=[WorkspaceOperationWorkflow]),
        Worker(client, task_queue=names.PROGRAM_QUEUE, activities=list(WorkspaceActivities(s.ops).activities().values())),
    ):
        inp = s.inp(Op.WORKSPACE_OPERATION_CREATE, repo_url=url, sealed_for=("t1", ""))
        out = await client.execute_workflow(
            names.WORKSPACE_OPERATION_WORKFLOW, inp, id=operation_workflow_id(inp.operation_id), task_queue=names.TASK_QUEUE,
            result_type=workspace_pb2.WorkspaceOperationResult,
        )
        assert not out.HasField("error") and json_format.MessageToDict(out.result)["workspace"]["workspace_id"] == "w1"
        history = await client.get_workflow_handle(operation_workflow_id(inp.operation_id)).fetch_history()
        raw = b"".join(e.SerializeToString() for e in history.events)
        assert inp.sealed_credential.encode() in raw and SECRET.encode() not in raw, "事件历史里只有封起来的凭据，没有明文"
        await s.tool("repo_edit", {"path": "x.txt", "new": "x\n"})
        await s.run(Op.WORKSPACE_OPERATION_COMMIT, message="x")
        backend = await s.boxes.backend("local")
        backend.provider._faults.fail_next("create", ErrorCategory.TRANSIENT)  # type: ignore[attr-defined]
        push = s.inp(Op.WORKSPACE_OPERATION_PUSH, branch="agent/x")
        started = datetime.now(UTC)
        out = await asyncio.wait_for(client.execute_workflow(
            names.WORKSPACE_OPERATION_WORKFLOW, push, id=operation_workflow_id(push.operation_id), task_queue=names.TASK_QUEUE,
            result_type=workspace_pb2.WorkspaceOperationResult,
        ), timeout=60)
        assert out.error.category == error_pb2.ERROR_CATEGORY_TRANSIENT and out.error.code == "sandbox_unavailable", out
        assert (datetime.now(UTC) - started).total_seconds() < 5, "推送不自动重试"
