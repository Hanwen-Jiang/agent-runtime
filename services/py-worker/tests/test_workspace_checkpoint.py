"""docs/plan/running-task-interaction.md 第 2 节验收：打断 write_reversible 调用时用的工作副本检查点。

调用前在工作副本建一个检查点（GIT_INDEX_FILE 指向临时索引，git add -A 加 write-tree，不动分支与用户的索引）；打断时把文件
还原到检查点。检查点只经一个 runner 跑 git 命令：沙箱里的工作副本 Worker 碰不到文件，同一套命令在沙箱里跑。

- 还原后工作副本与调用前一致：改过的、删掉的、新建的、调用前就有的未跟踪文件。
- 用户的索引（已暂存的改动）与分支不变，不留下多余的文件、ref 或 stash。
- 撤不回的照实说：不是 git 仓库、被 .gitignore 忽略的文件、调用自己动了分支。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from agentruntime_core.providers.sandbox import ExecResult

GIT = shutil.which("git")
if GIT is None and os.environ.get("AR_ALLOW_MISSING_TOOLS") == "1":
    pytest.skip("缺 git", allow_module_level=True)
assert GIT is not None, "这组测试要 PATH 上的 git"


class Runner:
    """在工作副本里跑命令，记下跑过的命令。"""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.commands: list[tuple[list[str], dict[str, str]]] = []

    async def __call__(self, argv: Sequence[str], env: Mapping[str, str] | None = None) -> ExecResult:
        self.commands.append((list(argv), dict(env or {})))
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=self.cwd, env={**os.environ, **(env or {})}, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return ExecResult(exit_code=proc.returncode, stdout=out, stderr=err, timed_out=False)


def git(cwd: Path, *args: str) -> str:
    import subprocess

    done = subprocess.run([GIT, "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, capture_output=True, check=True)
    return done.stdout.decode("utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "tracked.txt").write_text("原来的\n", "utf-8")
    (root / "gone.txt").write_text("会被删掉\n", "utf-8")
    (root / "staged.txt").write_text("提交时的\n", "utf-8")
    (root / ".gitignore").write_text("ignored.log\n", "utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "first")
    return root


def snapshot(root: Path) -> dict[str, bytes]:
    """工作副本里除 .git 以外的全部文件。"""
    return {str(p.relative_to(root)).replace("\\", "/"): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.relative_to(root).parts}


def index_bytes(root: Path) -> bytes:
    return (root / ".git" / "index").read_bytes()


def refs(root: Path) -> str:
    return git(root, "for-each-ref") + git(root, "stash", "list") + git(root, "rev-parse", "HEAD") + git(root, "symbolic-ref", "HEAD")


async def checkpoint(root: Path):
    from agentruntime_worker.tools.checkpoint import create_checkpoint

    return await create_checkpoint(Runner(root))


async def restore(root: Path, point):
    from agentruntime_worker.tools.checkpoint import restore_checkpoint

    return await restore_checkpoint(Runner(root), point)


async def test_restores_changed_and_deleted_files_and_removes_new_ones(repo: Path) -> None:
    before = snapshot(repo)
    point = await checkpoint(repo)
    assert point is not None
    (repo / "tracked.txt").write_text("被这次调用改了\n", "utf-8")
    (repo / "gone.txt").unlink()
    (repo / "made_by_the_call.txt").write_text("新建的\n", "utf-8")
    (repo / "new_dir" / "deep").mkdir(parents=True)
    (repo / "new_dir" / "deep" / "file.txt").write_text("新建的\n", "utf-8")
    restored = await restore(repo, point)
    assert restored.ok is True
    assert snapshot(repo) == before
    assert not (repo / "new_dir").exists(), "新建文件留下的空目录也不留"


async def test_keeps_the_uncommitted_work_from_before_the_call(repo: Path) -> None:
    """调用前已经有的改动与未跟踪文件是用户或前面的步骤做的，要还原到那个样子，不是还原到最后一次提交。"""
    (repo / "tracked.txt").write_text("调用前改的\n", "utf-8")
    (repo / "untracked.txt").write_text("调用前新建的\n", "utf-8")
    before = snapshot(repo)
    point = await checkpoint(repo)
    (repo / "tracked.txt").write_text("被这次调用改了\n", "utf-8")
    (repo / "untracked.txt").write_text("也被改了\n", "utf-8")
    await restore(repo, point)
    assert snapshot(repo) == before


async def test_a_file_name_with_spaces_or_chinese_survives(repo: Path) -> None:
    before = snapshot(repo)
    point = await checkpoint(repo)
    (repo / "有 空格 的文件.txt").write_text("新建的\n", "utf-8")
    await restore(repo, point)
    assert snapshot(repo) == before


async def test_binary_files_are_restored_byte_for_byte(repo: Path) -> None:
    data = bytes(range(256)) * 16
    (repo / "blob.bin").write_bytes(data)
    point = await checkpoint(repo)
    (repo / "blob.bin").write_bytes(b"\x00")
    await restore(repo, point)
    assert (repo / "blob.bin").read_bytes() == data


async def test_the_users_index_and_branch_stay_as_they_were(repo: Path) -> None:
    """已暂存的改动还是已暂存；分支、HEAD、ref、stash 都不变。"""
    (repo / "staged.txt").write_text("暂存的\n", "utf-8")
    git(repo, "add", "staged.txt")
    index_before, refs_before, status_before = index_bytes(repo), refs(repo), git(repo, "status", "--porcelain")
    point = await checkpoint(repo)
    assert index_bytes(repo) == index_before, "建检查点不动用户的索引"
    assert refs(repo) == refs_before, "建检查点不留 ref 与 stash"
    (repo / "tracked.txt").write_text("被这次调用改了\n", "utf-8")
    await restore(repo, point)
    assert refs(repo) == refs_before
    assert git(repo, "status", "--porcelain") == status_before
    assert git(repo, "diff", "--cached", "--name-only").strip() == "staged.txt"


async def test_making_a_checkpoint_changes_nothing_a_user_can_see(repo: Path) -> None:
    (repo / "tracked.txt").write_text("调用前改的\n", "utf-8")
    before, status = snapshot(repo), git(repo, "status", "--porcelain")
    await checkpoint(repo)
    assert snapshot(repo) == before
    assert git(repo, "status", "--porcelain") == status
    assert [p.name for p in (repo / ".git").glob("*checkpoint*")] == [], "临时索引用完就删"


async def test_it_only_uses_a_temporary_index() -> None:
    """靠命令本身保证不碰用户的状态：改索引的命令都带 GIT_INDEX_FILE，不用 stash、reset、checkout <分支>、clean。"""
    import subprocess
    import tempfile

    from agentruntime_worker.tools.checkpoint import create_checkpoint, restore_checkpoint

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        subprocess.run([GIT, "init", "-q"], cwd=root, check=True)
        (root / "a.txt").write_text("x", "utf-8")
        run = Runner(root)
        point = await create_checkpoint(run)
        (root / "b.txt").write_text("y", "utf-8")
        await restore_checkpoint(run, point)
        for argv, env in run.commands:
            words = " ".join(argv)
            for banned in ("stash", " reset", " clean", "checkout "):
                assert banned not in words, words
            if any(w in argv for w in ("add", "read-tree", "write-tree", "checkout-index")):
                assert "GIT_INDEX_FILE" in env, words


async def test_ignored_files_are_left_alone_and_that_is_said(repo: Path) -> None:
    """git add -A 看不到被忽略的文件：这次调用改的撤不回，要照实说。"""
    (repo / "ignored.log").write_text("调用前\n", "utf-8")
    point = await checkpoint(repo)
    (repo / "ignored.log").write_text("被这次调用改了\n", "utf-8")
    restored = await restore(repo, point)
    assert restored.ok is True
    assert (repo / "ignored.log").read_text("utf-8") == "被这次调用改了\n"
    assert ".gitignore" in restored.note or "忽略" in restored.note


async def test_a_call_that_moved_the_branch_is_reported(repo: Path) -> None:
    """文件还原到检查点；分支不替人挪回去（可能已经推送了），照实说。"""
    before = snapshot(repo)
    head = git(repo, "rev-parse", "HEAD")
    point = await checkpoint(repo)
    (repo / "tracked.txt").write_text("被这次调用改了\n", "utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "by the call")
    restored = await restore(repo, point)
    assert snapshot(repo) == before
    assert git(repo, "rev-parse", "HEAD") != head, "分支不挪回去"
    assert "分支" in restored.note or "提交" in restored.note


async def test_outside_a_git_repository_there_is_no_checkpoint(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x", "utf-8")
    assert await checkpoint(tmp_path) is None


async def test_a_repository_without_commits_works(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("x", "utf-8")
    before = snapshot(tmp_path)
    point = await checkpoint(tmp_path)
    (tmp_path / "a.txt").write_text("y", "utf-8")
    (tmp_path / "b.txt").write_text("z", "utf-8")
    await restore(tmp_path, point)
    assert snapshot(tmp_path) == before


async def test_a_failed_restore_says_so(repo: Path) -> None:
    """还原失败不能报成功：模型与用户都要知道改动还在。"""
    point = await checkpoint(repo)
    (repo / "tracked.txt").write_text("被这次调用改了\n", "utf-8")
    (repo / ".git" / "objects").rename(repo / ".git" / "objects.gone")
    restored = await restore(repo, point)
    assert restored.ok is False
    assert restored.note
