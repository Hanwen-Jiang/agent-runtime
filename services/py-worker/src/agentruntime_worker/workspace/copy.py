"""复制工作区而不复制整个仓库（docs/plan/feedback-2026-11-01.md N-10）。

挂卷的后端（工作区是 <工作区根>/<租户>/<workspace>，Worker 也挂着同一处）：Worker 直接在卷上复制，`.git/objects` 下的文件用硬链接
（git 的对象写好就不再改，打包与清理只删除、不就地改；只读的权限照原样），工作区的其余部分（工作树、index、refs、配置）照常复制，
所以未提交的改动与未跟踪的文件也都在，与之前经环境导出导入的结果相同。两个会话的工作树、分支与 index 各自独立；共享的只有不可变的
对象文件。不用 `git worktree`（分支与 refs 共享，一个会话检出的分支另一个不能检出）或 `--shared`（源仓库 gc 之后副本就坏了）。

源目录来自沙箱，不可信：不跟随符号链接（链接照原样建成链接），只链接、复制普通文件，设备文件与管道跳过；文件在复制途中被换成
链接也只会链接到链接本身。硬链接不了（跨文件系统等）时退回复制。总大小（含链接的）按工作区的上限核对。
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from agentruntime_core.errors import InvalidError

OBJECTS = (".git", "objects")


@dataclass
class CopyStats:
    files: int = 0
    copied_bytes: int = 0
    linked_bytes: int = 0
    # 工作树里的文件（不算 .git）：复制之后核对“不留空的副本”。
    worktree_files: int = 0


def copy_working_copy(src: Path, dst: Path, *, max_bytes: int) -> CopyStats:
    """把 src 的工作区复制进空的 dst（已存在的空目录）：.git/objects 硬链接，其余复制。"""
    stats = CopyStats()
    src, dst = Path(src), Path(dst)
    if not src.is_dir() or src.is_symlink():
        raise InvalidError("invalid_source", "源工作区的目录不在")
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(src, followlinks=False):
        rel = Path(root).relative_to(src)
        target = dst / rel
        for name in list(dirs):
            path = Path(root) / name
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                dirs.remove(name)  # 指向目录的链接：建成链接，不进去
                os.symlink(os.readlink(path), target / name)
                continue
            (target / name).mkdir(mode=stat.S_IMODE(st.st_mode) | 0o700, exist_ok=True)
        in_objects = rel.parts[:2] == OBJECTS
        for name in files:
            path = Path(root) / name
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                os.symlink(os.readlink(path), target / name)
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            stats.files += 1
            if rel.parts[:1] != (".git",):
                stats.worktree_files += 1
            if stats.copied_bytes + stats.linked_bytes + st.st_size > max_bytes:
                raise InvalidError("too_large", f"工作区超过上限 {max_bytes} 字节")
            if in_objects:
                try:
                    os.link(path, target / name, follow_symlinks=False)
                    stats.linked_bytes += st.st_size
                    continue
                except OSError:
                    pass  # 跨文件系统等：复制
            _copy_file(path, target / name, st)
            stats.copied_bytes += st.st_size
    return stats


def _copy_file(src: Path, dst: Path, st: os.stat_result) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(src, flags)
    with os.fdopen(fd, "rb") as reader, open(dst, "wb") as writer:
        shutil.copyfileobj(reader, writer, 1 << 20)
    os.chmod(dst, stat.S_IMODE(st.st_mode))
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))


__all__ = ["CopyStats", "copy_working_copy"]
