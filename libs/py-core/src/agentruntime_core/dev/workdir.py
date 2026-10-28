"""工作目录的导出、导入、清单、列目录与搜索（D-06；docs/plan/workspace-layer.md）：开发实现的 Sandbox 直接对本机目录做，
规则与 sandbox-agent 的 /v1/archive、/v1/manifest、/v1/list、/v1/grep 相同（services/go-sandbox-agent/internal/agent/archive.go）。
语义见 agentruntime_core.providers.sandbox 的 SandboxArchive、SandboxBrowse。

- 只认普通文件、目录与符号链接；权限位只留 0o777 以内；agent 的标记文件 .agentruntime 不进存档与清单。
- 搜索用 Python 的 re（agent 是 RE2：简单的写法两边一样，回溯引用之类只有这里认）。
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import stat
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO

from agentruntime_core.errors import InvalidError, NotFoundError
from agentruntime_core.providers.sandbox import DirEntry, FileEntry, GrepMatch

MARKER = ".agentruntime"
MAX_GREP_FILE = 1 << 20
MAX_GREP_MATCHES = 1000
MAX_LIST_ENTRIES = 1000
_DRIVE = re.compile(r"^[A-Za-z]:")


def link_inside(rel: str, target: str) -> bool:
    """rel（相对工作目录）处的链接目标 target 是否还在工作目录里。"""
    if not target or target.startswith("/") or "\\" in target or "\x00" in target or _DRIVE.match(target):
        return False
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
    return joined != ".." and not joined.startswith("../")


def clean_name(name: str) -> str:
    """tar 里的名字规范化成相对工作目录的路径；不合法时 invalid。"""
    if not name or "\\" in name or "\x00" in name or name.startswith("/") or _DRIVE.match(name):
        raise InvalidError("invalid_path", "存档里有绝对路径或不合法的名字")
    rel = posixpath.normpath(name.rstrip("/"))
    if rel == ".." or rel.startswith("../"):
        raise InvalidError("invalid_path", "存档里有出工作目录的路径")
    return rel


def walk(root: Path) -> Iterator[tuple[str, os.stat_result]]:
    """深度优先、每层按名字排序遍历（不跟随符号链接），给出相对路径与 lstat；跳过标记文件与设备、管道、套接字。
    父目录总在它的内容之前（导出的 tar 按这个顺序，解开时父目录先建）。"""

    def visit(directory: Path, prefix: str) -> Iterator[tuple[str, os.stat_result]]:
        for name in sorted(os.listdir(directory)):
            rel = f"{prefix}{name}"
            if rel == MARKER:
                continue
            full = directory / name
            st = os.lstat(full)
            if stat.S_ISDIR(st.st_mode):
                yield rel, st
                yield from visit(full, rel + "/")
            elif stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
                yield rel, st

    yield from visit(root, "")


def export_dir(root: Path, out: IO[bytes], *, max_bytes: int) -> int:
    """工作目录打成 tar 写进 out，返回写了多少字节。"""
    entries = list(walk(root))
    total = sum(st.st_size for _, st in entries if stat.S_ISREG(st.st_mode))
    if total > max_bytes:
        raise InvalidError("too_large", f"工作目录有 {total} 字节，超过上限 {max_bytes}")
    counter = _Counting(out)
    with tarfile.open(fileobj=counter, mode="w|", format=tarfile.PAX_FORMAT) as tar:  # type: ignore[call-overload]
        for rel, st in entries:
            info = tarfile.TarInfo(rel)
            info.mode = stat.S_IMODE(st.st_mode) & 0o777
            info.mtime = int(st.st_mtime)
            full = root / rel
            if stat.S_ISDIR(st.st_mode):
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif stat.S_ISLNK(st.st_mode):
                target = os.readlink(full).replace(os.sep, "/")
                if not link_inside(rel, target):
                    continue  # 指出工作目录的链接不进存档
                info.type, info.linkname, info.mode = tarfile.SYMTYPE, target, 0o777
                tar.addfile(info)
            else:
                info.size = st.st_size
                with open(full, "rb") as f:
                    tar.addfile(info, f)
    return counter.written


class _Counting:
    """数写了多少字节（out 可能不能 tell）。"""

    def __init__(self, out: IO[bytes]) -> None:
        self._out = out
        self.written = 0

    def write(self, data: bytes) -> int:
        self._out.write(data)
        self.written += len(data)
        return len(data)

    def flush(self) -> None:
        self._out.flush()


def _parents_are_dirs(root: Path, rel: str) -> None:
    parent = posixpath.dirname(rel)
    if not parent:
        return
    cur = root
    for part in parent.split("/"):
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            (root / parent).mkdir(parents=True, exist_ok=True)
            return
        if not stat.S_ISDIR(st.st_mode):
            raise InvalidError("invalid_path", "存档里的路径经过了符号链接或文件")


def import_dir(root: Path, src: IO[bytes], *, max_bytes: int) -> None:
    """把 tar 解到空的工作目录。"""
    if any(name != MARKER for name in os.listdir(root)):
        raise InvalidError("not_empty", "工作目录不是空的：只能把存档导进新建的环境")
    total = 0
    try:
        with tarfile.open(fileobj=src, mode="r|") as tar:
            for member in tar:
                rel = clean_name(member.name)
                if rel in (".", MARKER):
                    continue
                _parents_are_dirs(root, rel)
                target = root / rel
                perm = member.mode & 0o777
                if member.isdir():
                    try:
                        target.mkdir()
                    except FileExistsError:
                        pass
                    if target.is_symlink() or not target.is_dir():
                        raise InvalidError("invalid_path", "存档里的目录与已有的项重名")
                    os.chmod(target, perm | 0o700)
                elif member.isreg():
                    total += member.size
                    if total > max_bytes:
                        raise InvalidError("too_large", "存档超过上限")
                    data = tar.extractfile(member)
                    try:
                        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), perm | 0o600)
                    except FileExistsError:
                        raise InvalidError("invalid_path", "存档里有重名的文件") from None
                    with os.fdopen(fd, "wb") as f:
                        while data is not None and (chunk := data.read(1 << 20)):
                            f.write(chunk)
                    os.chmod(target, perm)
                elif member.issym():
                    if not link_inside(rel, member.linkname):
                        raise InvalidError("invalid_link", "存档里的符号链接指出了工作目录")
                    try:
                        os.symlink(member.linkname, target)
                    except FileExistsError:
                        raise InvalidError("invalid_path", "存档里有重名的项") from None
                    except OSError:
                        raise InvalidError("symlink_unsupported", "这里建不了符号链接") from None
                else:
                    raise InvalidError("unsupported_entry", "存档里只能有普通文件、目录与符号链接")
    except tarfile.TarError:
        raise InvalidError("invalid_archive", "不是合法的 tar") from None


def manifest_dir(root: Path) -> list[FileEntry]:
    """逐项的清单，按路径的字符串顺序排序（与 agent 相同）。"""
    out: list[FileEntry] = []
    for rel, st in walk(root):
        mode = stat.S_IMODE(st.st_mode) & 0o777
        if stat.S_ISDIR(st.st_mode):
            out.append(FileEntry(path=rel, type="dir", mode=mode))
        elif stat.S_ISLNK(st.st_mode):
            out.append(FileEntry(path=rel, type="symlink", mode=0, link=os.readlink(root / rel).replace(os.sep, "/")))
        else:
            h = hashlib.sha256()
            size = 0
            with open(root / rel, "rb") as f:
                while chunk := f.read(1 << 20):
                    h.update(chunk)
                    size += len(chunk)
            out.append(FileEntry(path=rel, type="file", mode=mode, size=size, sha256=h.hexdigest()))
    return sorted(out, key=lambda e: e.path)


def _resolve(root: Path, rel: str) -> Path:
    if not rel or rel == ".":
        return root
    if "\\" in rel or "\x00" in rel or rel.startswith("/") or _DRIVE.match(rel) or any(p in ("", ".", "..") for p in rel.split("/")):
        raise InvalidError("invalid_path", "路径必须是工作目录内的相对路径")
    return root.joinpath(*rel.split("/"))


def list_dir(root: Path, rel: str = "") -> list[DirEntry]:
    target = _resolve(root, rel)
    if target.is_symlink() or not target.is_dir():
        raise NotFoundError("dir_not_found", "没有这个目录")
    out: list[DirEntry] = []
    for name in sorted(os.listdir(target)):
        if target == root and name == MARKER:
            continue
        st = os.lstat(target / name)
        if stat.S_ISLNK(st.st_mode):
            out.append(DirEntry(name=name, type="symlink"))
        elif stat.S_ISDIR(st.st_mode):
            out.append(DirEntry(name=name, type="dir"))
        elif stat.S_ISREG(st.st_mode):
            out.append(DirEntry(name=name, type="file", size=st.st_size))
        if len(out) >= MAX_LIST_ENTRIES:
            break
    return out


def grep_dir(root: Path, pattern: str, *, rel: str = "", max_matches: int = 100) -> tuple[list[GrepMatch], bool]:
    try:
        compiled = re.compile(pattern)
    except re.error:
        raise InvalidError("invalid_pattern", "正则表达式不合法") from None
    _resolve(root, rel)
    prefix = "" if rel in ("", ".") else posixpath.normpath(rel)
    limit = min(max(1, int(max_matches or 100)), MAX_GREP_MATCHES)
    matches: list[GrepMatch] = []
    for path, st in walk(root):
        if ".git" in path.split("/") or not stat.S_ISREG(st.st_mode) or st.st_size > MAX_GREP_FILE:
            continue
        if prefix and path != prefix and not path.startswith(prefix + "/"):
            continue
        data = (root / path).read_bytes()
        if b"\x00" in data[:1024]:
            continue  # 二进制文件
        # 只按 \n 分行、去掉行尾的 \r（与 Go 的 bufio.ScanLines 相同；splitlines 还会在 \x0c 等处断开）。
        lines = data.decode("utf-8", errors="replace").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        for number, raw in enumerate(lines, start=1):
            line = raw[:-1] if raw.endswith("\r") else raw
            if compiled.search(line):
                matches.append(GrepMatch(path=path, line=number, text=line[:500]))
                if len(matches) >= limit:
                    return matches, True
    return matches, False


__all__ = ["MARKER", "clean_name", "export_dir", "grep_dir", "import_dir", "link_inside", "list_dir", "manifest_dir", "walk"]
