"""D-06e：Workspace API 在 Worker 一侧的操作（docs/plan/workspace-layer.md 第 5 节、第 6 节的验收 3、4、5）。

远端是本机的裸仓库（file://，只在 allow_local_remotes 时收；生产只收 https）。存档模式与挂卷两种后端各跑一遍主流程。

1. 建（克隆）：工作区的 head 是远端的 HEAD；工具（repo_read）看到克隆下来的文件；同名再建 conflict；没建成时撤掉这一行。
2. diff：会话里改的、新加的文件都在；内容与沙箱里 git diff 的输出一致；存档模式下会话的环境闲着时先存档、不回收它。
3. 提交：有改动时 committed，再提交一次没有改动时 committed 为 false、sha 不变。
4. 推送：分支不在策略里 denied；推到 agent/* 成功，远端有这个 sha；同一个提交再推一次 reconciled；gate 记录的批准人是发起的用户。
5. 凭据：封给别的租户或别的操作的凭据解不开（denied）；错误信息里没有凭据。
6. 别的租户同名的工作区 not_found；存档、删除（删除后快照与环境都没了，挂卷时目录也删了）。
7. 经 Temporal 跑工作流：结果原样带回；推送遇到暂时性的错误不重试，写成 transient 的结果。

D-06f（第 8 节，应用浏览与管理工作区）：
8. 列目录（不列 .git）、读文件（大文件只给大小、二进制不给内容、.git 拒绝）；提交记录（含 base..HEAD）。
9. 丢弃改动（未跟踪的删掉、.gitignore 忽略的留着）；同步（有改动时 workspace_dirty；干净时切到远端分支的最新提交）。
10. 从初始文件建（初始提交带作者）、从别的工作区复制（含未提交的改动，远端沿用，复制的能推送）；复制不能与另两种来源同给
    （克隆加文件、克隆到提交见 test_workspace_at_commit.py）。
11. diff 的逐文件行数、改名检测与当前分支。
12. 没配 Sandbox Service 时报 workspace_api_unavailable（SBX-01：工作区的操作一律在服务借的环境里做）。

环境经真的 Sandbox Service 进程借（service_setup）；服务与 Worker 共用业务库、对象存储与工作区根。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from google.protobuf import json_format
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentruntime.common.v1 import error_pb2
from agentruntime.workflow.v1 import workspace_pb2

from agentruntime_worker import names
from agentruntime_worker.workflows.workspace import WorkspaceOperationWorkflow, operation_workflow_id
from agentruntime_worker.workspace.operations import WorkspaceActivities, branch_allowed, check_branch, push_key

from . import service_setup
from .service_setup import KEY, S1, SECRET

__all__ = ["KEY", "S1", "SECRET", "Op", "Setup", "git", "make_remote"]

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


class Setup(service_setup.Setup):
    """存档模式（archive）或挂卷（mount，Worker 挂着工作区卷）；policy 是所有租户的策略。"""

    def __init__(self, tmp_path: Path, *, mode: str, policy: Mapping[str, Any] | None = None) -> None:
        super().__init__(tmp_path / "setup", mode=mode, policies={t: policy for t in ("t1", "t2")} if policy else None)


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
        writable = [(r["session_id"], r["state"]) for r in s.envs("t1") if r["writable"]]
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
    assert s.envs("t1") == []
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
    # 工作流名与队列（task、program）是固定的：用单独的连接，与别的用例在同一个客户端上登记的 Worker 不冲突。
    client = await Client.connect(temporal_env.client.service_client.config.target_host, namespace=temporal_env.client.namespace)
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
        s.svc.kill()  # 借环境时服务不在：transient
        push = s.inp(Op.WORKSPACE_OPERATION_PUSH, branch="agent/x")
        started = datetime.now(UTC)
        out = await asyncio.wait_for(client.execute_workflow(
            names.WORKSPACE_OPERATION_WORKFLOW, push, id=operation_workflow_id(push.operation_id), task_queue=names.TASK_QUEUE,
            result_type=workspace_pb2.WorkspaceOperationResult,
        ), timeout=60)
        assert out.error.category == error_pb2.ERROR_CATEGORY_TRANSIENT and out.error.code == "sandbox_service_unavailable", out
        assert (datetime.now(UTC) - started).total_seconds() < 5, "推送不自动重试"


# --- D-06f：应用浏览与管理工作区 ---


def py(code: str) -> dict[str, Any]:
    return {"argv": ["python", "-c", code]}


@pytest.mark.parametrize("mode", ["archive", "mount"])
async def test_browse_log_discard_and_sync(tmp_path: Path, mode: str) -> None:
    url, bare = make_remote(tmp_path)
    s = Setup(tmp_path, mode=mode)
    await s.run(Op.WORKSPACE_OPERATION_CREATE, repo_url=url)
    await s.tool("shell_run", py(
        "import os; os.makedirs('src', exist_ok=True); open('src/new.py', 'wb').write(b'n = 1\\n'); "
        "open('big.bin', 'wb').write(b'x' * 600000); open('blob.dat', 'wb').write(b'ab\\x00cd'); "
        "open('.git/info/exclude', 'ab').write(b'ignored.txt\\n'); open('ignored.txt', 'wb').write(b'keep me\\n'); "
        "open('app.py', 'ab').write(b'# changed\\n')"
    ))

    listed = await s.run(Op.WORKSPACE_OPERATION_LIST_FILES)
    by_name = {e["name"]: e for e in listed["entries"]}
    assert ".git" not in by_name and by_name["src"]["type"] == "FILE_TYPE_DIRECTORY" and by_name["README.md"]["type"] == "FILE_TYPE_FILE"
    assert by_name["README.md"]["size"] == str(len(b"# refund service\n")) and listed.get("truncated", False) is False
    assert [e["name"] for e in (await s.run(Op.WORKSPACE_OPERATION_LIST_FILES, path="src"))["entries"]] == ["new.py"]
    for bad in (".git", "src/../..", "/etc"):
        assert (await s.error(Op.WORKSPACE_OPERATION_LIST_FILES, path=bad)).category == error_pb2.ERROR_CATEGORY_INVALID
    assert (await s.error(Op.WORKSPACE_OPERATION_LIST_FILES, path="missing")).category == error_pb2.ERROR_CATEGORY_NOT_FOUND

    assert (await s.run(Op.WORKSPACE_OPERATION_READ_FILE, path="src/new.py")) == {"size": "6", "content": "n = 1\n"}
    assert (await s.run(Op.WORKSPACE_OPERATION_READ_FILE, path="big.bin")) == {"size": "600000", "too_large": True}
    assert (await s.run(Op.WORKSPACE_OPERATION_READ_FILE, path="blob.dat")) == {"size": "5", "binary": True}
    assert (await s.error(Op.WORKSPACE_OPERATION_READ_FILE, path=".git/HEAD")).category == error_pb2.ERROR_CATEGORY_INVALID
    for missing in ("nope.txt", "src"):
        assert (await s.error(Op.WORKSPACE_OPERATION_READ_FILE, path=missing)).category == error_pb2.ERROR_CATEGORY_NOT_FOUND

    log = (await s.run(Op.WORKSPACE_OPERATION_LOG))["commits"]
    assert [(c["subject"], c["author_name"], c["author_email"]) for c in log] == [("seed", "t", "t@example.com")] and log[0]["authored_at"]
    assert (await s.run(Op.WORKSPACE_OPERATION_LOG, base="origin/main")).get("commits", []) == [], "还没有比远端多的提交"
    assert (await s.error(Op.WORKSPACE_OPERATION_LOG, base="no-such-branch")).category == error_pb2.ERROR_CATEGORY_NOT_FOUND

    # 有改动时不能同步；丢弃改动之后能。
    other = tmp_path / "other"
    git("clone", "--quiet", str(bare), str(other))
    (other / "CHANGELOG.md").write_bytes(b"merged upstream\n")
    git("add", "-A", cwd=other)
    git("-c", "user.name=o", "-c", "user.email=o@example.com", "commit", "--quiet", "-m", "upstream", cwd=other)
    git("push", "--quiet", "origin", "main", cwd=other)
    dirty = await s.error(Op.WORKSPACE_OPERATION_SYNC, branch="main")
    assert dirty.category == error_pb2.ERROR_CATEGORY_CONFLICT and dirty.code == "workspace_dirty"
    discarded = await s.run(Op.WORKSPACE_OPERATION_DISCARD)
    assert discarded["head"] == git("rev-parse", "HEAD~1", cwd=other)
    names_after = {e["name"] for e in (await s.run(Op.WORKSPACE_OPERATION_LIST_FILES))["entries"]}
    assert names_after == {"README.md", "app.py", "ignored.txt"}, "未跟踪的删了，.gitignore 忽略的留着"
    assert (await s.run(Op.WORKSPACE_OPERATION_DIFF)).get("files", []) == []
    synced = await s.run(Op.WORKSPACE_OPERATION_SYNC, branch="main")
    assert synced == {"head": git("rev-parse", "HEAD", cwd=other), "branch": "main"}
    assert (await s.run(Op.WORKSPACE_OPERATION_READ_FILE, path="CHANGELOG.md"))["content"] == "merged upstream\n"

    await s.tool("repo_edit", {"path": "fix.py", "new": "fixed = True\n"})
    await s.run(Op.WORKSPACE_OPERATION_COMMIT, message="fix")
    ahead = (await s.run(Op.WORKSPACE_OPERATION_LOG, base="origin/main"))["commits"]
    assert [c["subject"] for c in ahead] == ["fix"], "比远端多一个提交"


async def test_create_from_files_or_from_another_workspace(tmp_path: Path) -> None:
    url, bare = make_remote(tmp_path)
    s = Setup(tmp_path, mode="archive")
    files = [{"path": "README.md", "content": "# 示例\n"}, {"path": "src/a.py", "content": "x = 1\n"}]
    created = (await s.run(Op.WORKSPACE_OPERATION_CREATE, workspace="w-sample", files=files, author_name="Alice", author_email="alice@example.com"))["workspace"]
    assert created["branch"] == "main" and created["head"]
    [first] = (await s.run(Op.WORKSPACE_OPERATION_LOG, workspace="w-sample"))["commits"]
    assert (first["subject"], first["author_name"]) == ("Initial commit", "Alice")
    assert (await s.run(Op.WORKSPACE_OPERATION_READ_FILE, workspace="w-sample", path="src/a.py"))["content"] == "x = 1\n"
    for bad in ([{"path": ".git/config", "content": "x"}], [{"path": "a", "content": "1"}, {"path": "a", "content": "2"}], [{"path": "../x", "content": "1"}]):
        assert (await s.error(Op.WORKSPACE_OPERATION_CREATE, workspace="w-bad", files=bad)).category == error_pb2.ERROR_CATEGORY_INVALID
    # files 可以与 repo_url 同给（test_workspace_at_commit.py），不能与 from_workspace_id 同给。
    both = await s.error(Op.WORKSPACE_OPERATION_CREATE, workspace="w-bad", from_workspace_id="w-sample", files=files)
    assert both.code == "conflicting_sources"
    assert await s.archiver.get("t1", "w-bad") is None, "参数不对时不留行"

    await s.run(Op.WORKSPACE_OPERATION_CREATE, workspace="w-base", repo_url=url)
    await s.tool("repo_edit", {"path": "draft.md", "new": "not committed yet\n"}, workspace="w-base")
    copied = (await s.run(Op.WORKSPACE_OPERATION_CREATE, workspace="w-copy", from_workspace_id="w-base"))["workspace"]
    assert copied["repo_url"] == url and copied["head"] == git("rev-parse", "main", cwd=bare) and copied["branch"] == "main"
    assert (await s.run(Op.WORKSPACE_OPERATION_DIFF, workspace="w-copy"))["files"] == ["draft.md"], "复制的含未提交的改动"
    await s.run(Op.WORKSPACE_OPERATION_COMMIT, workspace="w-copy", message="from the copy")
    pushed = await s.run(Op.WORKSPACE_OPERATION_PUSH, workspace="w-copy", branch="agent/copy")
    assert git("rev-parse", "agent/copy", cwd=bare) == pushed["sha"]
    assert (await s.run(Op.WORKSPACE_OPERATION_DIFF, workspace="w-base"))["files"] == ["draft.md"], "源工作区不受影响"
    missing = await s.error(Op.WORKSPACE_OPERATION_CREATE, workspace="w-copy2", from_workspace_id="w-nope")
    assert missing.category == error_pb2.ERROR_CATEGORY_NOT_FOUND and await s.archiver.get("t1", "w-copy2") is None
    other_tenant = await s.error(Op.WORKSPACE_OPERATION_CREATE, workspace="w-copy3", tenant="t2", from_workspace_id="w-base")
    assert other_tenant.category == error_pb2.ERROR_CATEGORY_NOT_FOUND, "不能复制别的租户的工作区"


async def test_diff_counts_lines_detects_renames_and_reports_the_branch(tmp_path: Path) -> None:
    url, _ = make_remote(tmp_path)
    s = Setup(tmp_path, mode="mount")
    await s.run(Op.WORKSPACE_OPERATION_CREATE, repo_url=url)
    await s.tool("shell_run", py(
        "import os; os.rename('app.py', 'refund.py'); open('README.md', 'ab').write(b'one\\ntwo\\n'); open('logo.png', 'wb').write(b'\\x89PNG\\x00\\x01')"
    ))
    plain = await s.run(Op.WORKSPACE_OPERATION_DIFF)
    assert plain["branch"] == "main" and sorted(plain["files"]) == ["README.md", "app.py", "logo.png", "refund.py"]
    stats = {st["path"]: st for st in plain["stats"]}
    assert stats["README.md"]["added"] == 2 and stats["README.md"].get("deleted", 0) == 0
    assert stats["logo.png"]["binary"] is True and stats["app.py"]["deleted"] == 2
    renamed = await s.run(Op.WORKSPACE_OPERATION_DIFF, find_renames=True)
    assert sorted(renamed["files"]) == ["README.md", "logo.png", "refund.py"] and "rename from app.py" in renamed["diff"]
    assert [st["path"] for st in renamed["stats"]] == renamed["files"], "stats 与 files 同序"


async def test_the_api_needs_the_sandbox_service(tmp_path: Path) -> None:
    """没配 Sandbox Service（AR_SANDBOX_SERVICE_ADDRESS）时 Workspace API 不可用，不在 Worker 的工作区目录里自己做（SBX-01）。"""
    from agentruntime_worker.config import WorkerConfig
    from agentruntime_worker.tools import workspace_operations

    config = WorkerConfig(tool_workspace_root=tmp_path / "workspaces", tool_shell_sandbox="host", workspace_allow_local_remotes=True)
    assert workspace_operations(config) is None
    unavailable = WorkspaceActivities(workspace_operations(config))
    res = await unavailable.workspace_operation(Setup.inp(Op.WORKSPACE_OPERATION_DIFF))
    assert res.error.code == "workspace_api_unavailable" and "AR_SANDBOX_SERVICE_ADDRESS" in res.error.message
