"""工作副本的检查点（docs/plan/running-task-interaction.md 第 2 节）：write_reversible 的调用之前建一个，被立刻打断时把文件还原回去。

- 只经一个 runner 跑 git 命令：沙箱里的工作副本 Worker 碰不到文件，同一套命令在沙箱里跑。
- 建：GIT_INDEX_FILE 指向临时索引，git add -A 加 write-tree，得到一棵树。不动分支与用户的索引，不留 ref 与 stash（树是悬空对象，git gc 会清）。
- 还原：先用临时索引找出调用新建的文件（当前有、树里没有）并删掉，再把树读进临时索引、checkout-index 写回工作副本。
- 撤不回的照实说：被 .gitignore 忽略的文件看不到；调用自己提交或换了分支时文件还原，分支不替人挪回去（可能已经推送了）。
- 工作目录不是仓库的根（包括不是仓库）时没有检查点：不碰外层仓库。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from agentruntime_core.errors import ProviderError
from agentruntime_core.providers.sandbox import ExecResult

from agentruntime_worker.workflows.interrupts import NO_CHECKPOINT, RESTORED, UNDO_FAILED

Runner = Callable[..., Awaitable[ExecResult]]
"""在工作副本里跑命令：run(argv, env=None)。"""

# 同 Workspace API 的 git 命令（workspace/operations.py）：工作区的属主可能不是跑命令的用户；文件按字节存取，不换行尾。
_CONFIG = ("-c", "safe.directory=*", "-c", "core.autocrlf=false")
# 删除临时文件：只有 git 时也能删（Git for Windows 自带 sh）。
_REMOVE = "alias.ar-remove=!f() { rm -f \"$@\"; }; f"
_RM_BATCH = 100


class UndoReport(asyncio.CancelledError):
    """工具被取消、撤销做完了：还是取消，另带撤销的结果（workflows/interrupts.py 的 RESTORED、UNDO_FAILED、NO_CHECKPOINT、NOT_NEEDED）。"""

    def __init__(self, undo: str) -> None:
        super().__init__(undo)
        self.undo = undo


@dataclass(frozen=True)
class Checkpoint:
    tree: str
    head: str
    git_dir: str


@dataclass(frozen=True)
class Restored:
    ok: bool
    note: str


async def _git(run: Runner, *args: str, env: Mapping[str, str] | None = None) -> ExecResult | None:
    """跑一条 git 命令；启动不了时 None。"""
    try:
        return await run(["git", *_CONFIG, *args], env)
    except (OSError, ProviderError):
        return None


def _text(result: ExecResult) -> str:
    return result.stdout.decode("utf-8", "surrogateescape").strip()


def _ok(result: ExecResult | None) -> bool:
    return result is not None and result.exit_code == 0


def _temp_index(git_dir: str) -> dict[str, str]:
    return {"GIT_INDEX_FILE": f"{git_dir}/ar-checkpoint-index-{os.urandom(6).hex()}"}


async def _remove(run: Runner, env: Mapping[str, str]) -> None:
    await _git(run, "-c", _REMOVE, "ar-remove", env["GIT_INDEX_FILE"])


async def create_checkpoint(run: Runner) -> Checkpoint | None:
    """给现在的工作副本建检查点；做不到（不是仓库的根、git 出错）时 None，调用照常执行，打断时照实说没能撤销。"""
    where = await _git(run, "rev-parse", "--absolute-git-dir", "--show-cdup")
    if not _ok(where):
        return None
    assert where is not None
    lines = where.stdout.decode("utf-8", "surrogateescape").splitlines()
    if len(lines) < 1 or (len(lines) > 1 and lines[1].strip()):
        return None
    git_dir = lines[0].strip()
    env = _temp_index(git_dir)
    try:
        if not _ok(await _git(run, "add", "-A", env=env)):
            return None
        tree = await _git(run, "write-tree", env=env)
        if not _ok(tree):
            return None
        assert tree is not None
    finally:
        await _remove(run, env)
    head = await _git(run, "rev-parse", "--verify", "-q", "HEAD")
    return Checkpoint(tree=_text(tree), head=_text(head) if _ok(head) and head is not None else "", git_dir=git_dir)


async def _delete_new_files(run: Runner, point: Checkpoint, env: Mapping[str, str]) -> bool:
    """调用新建的文件：当前有、树里没有。git rm 经临时索引删，顺带删掉空目录；用户的索引不动。"""
    if not _ok(await _git(run, "add", "-A", env=env)):
        return False
    added = await _git(run, "diff", "--cached", "--name-only", "--diff-filter=A", "--no-renames", "-z", point.tree, env=env)
    if not _ok(added):
        return False
    assert added is not None
    names = [n.decode("utf-8", "surrogateescape") for n in added.stdout.split(b"\x00") if n]
    for start in range(0, len(names), _RM_BATCH):
        if not _ok(await _git(run, "rm", "-f", "-q", "--", *names[start : start + _RM_BATCH], env=env)):
            return False
    return True


async def restore_checkpoint(run: Runner, point: Checkpoint) -> Restored:
    """把工作副本的文件还原到检查点。ok 为 False 时改动可能还在，note 说明。"""
    env = _temp_index(point.git_dir)
    try:
        if not await _delete_new_files(run, point, env):
            return Restored(False, "没能找出或删掉这次调用新建的文件")
        if not _ok(await _git(run, "read-tree", point.tree, env=env)):
            return Restored(False, "读不出检查点（git 对象可能坏了）")
        if not _ok(await _git(run, "checkout-index", "-a", "-f", env=env)):
            return Restored(False, "没能把文件写回工作副本")
    finally:
        await _remove(run, env)
    notes = ["被 .gitignore 忽略的文件没有还原"]
    head = await _git(run, "rev-parse", "--verify", "-q", "HEAD")
    if (_text(head) if _ok(head) and head is not None else "") != point.head:
        notes.append("这次调用新建了提交或换了分支，文件已还原，分支没有挪回去")
    return Restored(True, "；".join(notes))


async def undo_with(run: Runner, point: Checkpoint | None) -> UndoReport:
    """被取消之后：还原到检查点，返回要抛的 UndoReport。还原本身不再被取消打断。"""
    if point is None:
        return UndoReport(NO_CHECKPOINT)
    try:
        restored = await asyncio.shield(restore_checkpoint(run, point))
    except (OSError, ProviderError):
        return UndoReport(UNDO_FAILED)
    return UndoReport(RESTORED if restored.ok else UNDO_FAILED)

