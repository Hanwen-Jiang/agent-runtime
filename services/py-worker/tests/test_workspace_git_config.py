"""G-03 第 9 条（Runtime 一侧）：带凭据的 git 不读工作副本的 .git/config（docs/plan/all-plans-dag-2026-11-03.md 第 8 节）。

工作副本的 .git/config 是 Agent 改得了的（shell_run、repo_edit）。凭据经 GIT_CONFIG_* 环境变量交给 git（http.<远端>.extraHeader），
git 读配置时起的子进程都继承这些变量：`url.<ext::命令>.insteadOf` 加 `protocol.ext.allow=always` 把业务库里记的远端改写成
ext 传输，推送前的对账（ls-remote）就执行了 Agent 写的命令，Authorization 头落进它能读的文件。同类的还有 credential.helper、
http.<url>.sslVerify、sslCAInfo、curloptResolve、proxy 等，逐项覆盖靠不住：要求连远端的 git 根本不读工作副本的配置。

1. 推送：工作副本的配置里有 ext 的改写时，Agent 的命令不执行，凭据不落进它能读的文件，错误信息里也没有凭据。
2. 同步：同上。
3. 正常的路径不受影响：同步（--depth 50，工作副本变成浅的）之后提交、推送，远端有这个提交。
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

from agentruntime_core.errors import ProviderError

from .test_workspace_operations import SECRET, Op, Setup, git, make_remote

HTTPS_REMOTE = "https://example.test/acme/refund.git"


def ext_payload(leak: Path) -> str:
    """ext 传输的命令：把环境变量写进 leak。ext:: 按空格分参数，字面的空格写 "% "；URL 剩下的部分接在最后一个参数后面。"""
    python = sys.executable.replace("\\", "/").replace("%", "%%").replace(" ", "% ")
    code = "import% os,sys;open(sys.argv[1],'w').write(str(dict(os.environ)))"
    return f"ext::{python} -c {code} {leak.as_posix().replace('%', '%%').replace(' ', '% ')} rest:"


async def plant(s: Setup, leak: Path) -> None:
    """Agent 在会话里改工作副本的 .git/config。"""
    config = f'[url "{ext_payload(leak)}"]\n\tinsteadOf = https://example.test/\n[protocol "ext"]\n\tallow = always\n'
    code = f"open('.git/config', 'a', encoding='utf-8').write({config!r})"
    out = await s.tool("shell_run", {"argv": ["python", "-c", code]})
    assert out["exit_code"] == 0, out


async def failure(s: Setup, op: int, **kw: object) -> str:
    """操作失败时的错误信息：不管是写进结果的，还是暂时性的（连不上远端，交给工作流重试、抛出来的）。"""
    try:
        return (await s.error(op, **kw)).message
    except ProviderError as err:
        return err.message


def assert_not_leaked(leak: Path, message: str) -> None:
    token = base64.b64encode(f"bot:{SECRET}".encode()).decode()
    assert not leak.exists(), "工作副本配置里的命令不应执行"
    for secret in (SECRET, token):
        assert secret not in message


@pytest.mark.parametrize("mode", ["archive", "mount"])
async def test_push_does_not_run_commands_from_the_working_copy_config(tmp_path: Path, mode: str) -> None:
    url, _ = make_remote(tmp_path)
    s = Setup(tmp_path, mode=mode)
    await s.run(Op.WORKSPACE_OPERATION_CREATE, repo_url=url)
    leak = tmp_path / "leak.txt"
    await plant(s, leak)
    await s.archiver.update("t1", "w1", repo_url=HTTPS_REMOTE)
    assert_not_leaked(leak, await failure(s, Op.WORKSPACE_OPERATION_PUSH, branch="agent/fix", sealed_for=("t1", "")))


@pytest.mark.parametrize("mode", ["archive", "mount"])
async def test_sync_does_not_run_commands_from_the_working_copy_config(tmp_path: Path, mode: str) -> None:
    url, _ = make_remote(tmp_path)
    s = Setup(tmp_path, mode=mode)
    await s.run(Op.WORKSPACE_OPERATION_CREATE, repo_url=url)
    leak = tmp_path / "leak.txt"
    await plant(s, leak)
    await s.archiver.update("t1", "w1", repo_url=HTTPS_REMOTE)
    assert_not_leaked(leak, await failure(s, Op.WORKSPACE_OPERATION_SYNC, branch="main", sealed_for=("t1", "")))


@pytest.mark.parametrize("mode", ["archive", "mount"])
async def test_push_after_a_shallow_sync_still_works(tmp_path: Path, mode: str) -> None:
    url, bare = make_remote(tmp_path)
    s = Setup(tmp_path, mode=mode)
    await s.run(Op.WORKSPACE_OPERATION_CREATE, repo_url=url)
    # 远端另有一个 60 个提交的分支：同步只取最近的 50 个，工作副本缺更早的父提交，要靠 shallow 记下边界。
    seed = tmp_path / "seed"
    git("checkout", "--quiet", "-b", "long", cwd=seed)
    for i in range(60):
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--quiet", "--allow-empty", "-m", f"long {i}", cwd=seed)
    git("push", "--quiet", str(bare), "long", cwd=seed)
    synced = await s.run(Op.WORKSPACE_OPERATION_SYNC, branch="long", sealed_for=("t1", ""))
    assert synced["head"] == git("rev-parse", "long", cwd=bare)
    commits = (await s.run(Op.WORKSPACE_OPERATION_LOG, limit=200))["commits"]
    assert len(commits) == 50, "提交记录停在 shallow 的边界上（shallow 交回了工作副本）"
    await s.tool("repo_edit", {"path": "docs/after-sync.md", "new": "after sync\n"})
    commit = await s.run(Op.WORKSPACE_OPERATION_COMMIT, message="同步之后的改动")
    pushed = await s.run(Op.WORKSPACE_OPERATION_PUSH, branch="agent/after-sync", sealed_for=("t1", ""))
    assert pushed == {"sha": commit["sha"], "branch": "agent/after-sync", "reconciled": False}
    assert git("rev-parse", "agent/after-sync", cwd=bare) == commit["sha"]
