"""Extended Tier-1 core repository tools: repo_glob, repo_git (read-only), and apply_patch (MAT-TOOL-L1)."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from agentruntime_core.enums import Effect
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError, TransientError
from agentruntime_core.providers.tool_driver import ToolInvocation, ToolSpec

_ALLOWED_GIT_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "blame", "rev-parse", "branch", "ls-files"})
_FORBIDDEN_GIT_FLAGS = frozenset({"-c", "--git-dir", "--work-tree", "--exec-path", "--ext-diff", "--output", "-o"})
_FORBIDDEN_BRANCH_FLAGS = frozenset({"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "--set-upstream-to", "-u", "--unset-upstream"})

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class _FilePatch:
    path: str
    action: str  # "modified" | "added" | "deleted"
    hunks: list[tuple[int, list[str]]]


def _validate_glob_pattern(pattern: str) -> str:
    clean = pattern.strip()
    if not clean or clean.startswith("/") or "\\" in clean or ".." in Path(clean).parts:
        raise InvalidError("invalid_glob", "glob 模式不能为空、不能包含反斜杠、.. 或以 / 开头")
    return clean


def _match_glob(rel_posix: str, pattern: str) -> bool:
    if PurePosixPath(rel_posix).match(pattern):
        return True
    return fnmatch.fnmatch(rel_posix, pattern)


def _clean_patch_path(raw: str) -> str:
    text = raw.strip().split("\t", 1)[0].strip()
    if text in ("/dev/null", "dev/null"):
        return "/dev/null"
    if text.startswith(("a/", "b/")):
        text = text[2:]
    return text


def _parse_unified_patch(patch_text: str) -> list[_FilePatch]:
    lines = patch_text.splitlines()
    if not lines:
        raise InvalidError("invalid_patch", "patch 不能为空")

    patches: list[_FilePatch] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.startswith(("diff --git ", "index ", "--- ")):
            if line.startswith("--- "):
                old_raw = _clean_patch_path(line[4:])
                if idx + 1 >= len(lines) or not lines[idx + 1].startswith("+++ "):
                    raise InvalidError("invalid_patch", f"缺少配对的 +++ 文件头（第 {idx + 1} 行）")
                new_raw = _clean_patch_path(lines[idx + 1][4:])
                idx += 2
                if old_raw == "/dev/null" and new_raw == "/dev/null":
                    raise InvalidError("invalid_patch", "无效文件补丁：旧新路径均为空")
                if old_raw == "/dev/null":
                    action, rel_path = "added", new_raw
                elif new_raw == "/dev/null":
                    action, rel_path = "deleted", old_raw
                else:
                    action, rel_path = "modified", new_raw

                hunks: list[tuple[int, list[str]]] = []
                while idx < len(lines):
                    cur = lines[idx]
                    if cur.startswith(("diff --git ", "--- ")):
                        break
                    m = _HUNK_RE.match(cur)
                    if not m:
                        if cur.startswith("\\ No newline at end of file") or cur == "":
                            idx += 1
                            continue
                        raise InvalidError("invalid_patch", f"无法解析 hunk 头：{cur}")
                    old_start = int(m.group(1))
                    idx += 1
                    body: list[str] = []
                    while idx < len(lines):
                        hline = lines[idx]
                        if hline.startswith(("diff --git ", "--- ", "@@ ")):
                            break
                        if hline.startswith("\\ No newline at end of file"):
                            idx += 1
                            continue
                        if not hline.startswith((" ", "+", "-")):
                            if hline == "" and (idx + 1 == len(lines) or lines[idx + 1].startswith(("diff --git ", "--- "))):
                                idx += 1
                                break
                            raise InvalidError("invalid_patch", f"非法 patch 行前缀：{hline[:40]}")
                        body.append(hline)
                        idx += 1
                    hunks.append((old_start, body))
                if not hunks:
                    raise InvalidError("invalid_patch", f"文件 {rel_path} 没有任何 hunk")
                patches.append(_FilePatch(path=rel_path, action=action, hunks=hunks))
                continue
        idx += 1

    if not patches:
        raise InvalidError("invalid_patch", "未在 patch 中发现有效的 --- / +++ diff 块")
    return patches


def _apply_hunks(original: str | None, patch: _FilePatch) -> bytes | None:
    if patch.action == "added":
        if original is not None:
            raise InvalidError("patch_conflict", f"文件已存在，无法新增：{patch.path}")
        new_lines: list[str] = []
        for _, body in patch.hunks:
            for line in body:
                if line.startswith("-"):
                    raise InvalidError("patch_conflict", f"新增文件 {patch.path} 的 hunk 不能含删除行")
                if line.startswith(("+", " ")):
                    new_lines.append(line[1:])
        return ("\n".join(new_lines) + ("\n" if new_lines else "")).encode("utf-8")

    if original is None:
        raise NotFoundError("file_not_found", f"待修改文件不存在：{patch.path}")

    had_trailing_nl = original.endswith("\n")
    src_lines = original.splitlines()
    # Build edited copy sequentially keeping running offset from previous hunks
    offset = 0
    for old_start, body in patch.hunks:
        expected = [line[1:] for line in body if line.startswith((" ", "-"))]
        replacement = [line[1:] for line in body if line.startswith((" ", "+"))]

        hint_idx = max(0, (old_start - 1) + offset)
        match_idx = _find_hunk_position(src_lines, expected, hint_idx, patch.path)
        src_lines = src_lines[:match_idx] + replacement + src_lines[match_idx + len(expected) :]
        offset += len(replacement) - len(expected)

    if patch.action == "deleted":
        if src_lines:
            raise InvalidError("patch_conflict", f"删除文件 {patch.path} 后仍有未删除内容")
        return None

    joined = "\n".join(src_lines)
    if (had_trailing_nl or src_lines) and joined != "":
        joined += "\n"
    return joined.encode("utf-8")


def _find_hunk_position(lines: list[str], expected: list[str], hint_idx: int, path: str) -> int:
    span = len(expected)
    if span == 0:
        return min(hint_idx, len(lines))
    if 0 <= hint_idx and hint_idx + span <= len(lines) and lines[hint_idx : hint_idx + span] == expected:
        return hint_idx
    candidates = [i for i in range(len(lines) - span + 1) if lines[i : i + span] == expected]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # Pick unique closest candidate only if strictly closer than all others
        ranked = sorted((abs(c - hint_idx), c) for c in candidates)
        if ranked[0][0] < ranked[1][0]:
            return ranked[0][1]
        raise InvalidError("patch_conflict", f"{path} 的 hunk 匹配多处相同上下文，请提供更精确行号或上下文")
    raise InvalidError("patch_conflict", f"{path} 的 hunk 上下文与当前文件内容不匹配")


class RepoOpsTools:
    """Hosts repo_glob, repo_git (read-only), and atomic apply_patch across mounted & archived workspaces."""

    def __init__(self, workspaces: Any) -> None:
        self.w = workspaces

    def tools(self) -> list[tuple[ToolSpec, Any]]:
        return [
            (
                ToolSpec(
                    name="repo_glob",
                    version="v1",
                    effect=Effect.READ,
                    description="按 glob 模式快速查找工作区中的文件路径（如 **/*.py、src/**/*.ts）",
                    input_schema={
                        "type": "object",
                        "required": ["pattern"],
                        "properties": {
                            "pattern": {"type": "string", "minLength": 1, "maxLength": 256},
                            "path": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                        },
                        "additionalProperties": False,
                    },
                    output_schema={"type": "object"},
                    permissions=frozenset(),
                    timeout=timedelta(seconds=30),
                ),
                self.repo_glob,
            ),
            (
                ToolSpec(
                    name="apply_patch",
                    version="v1",
                    effect=Effect.WRITE_REVERSIBLE,
                    description="按 unified diff 原子修改、新增或删除工作区中的一个或多个文件（任意 hunk 校验失败则全部不写入）",
                    input_schema={
                        "type": "object",
                        "required": ["patch"],
                        "properties": {
                            "patch": {"type": "string", "minLength": 1, "maxLength": 262_144},
                        },
                        "additionalProperties": False,
                    },
                    output_schema={"type": "object"},
                    permissions=frozenset(),
                    timeout=timedelta(seconds=30),
                ),
                self.apply_patch,
            ),
            (
                ToolSpec(
                    name="repo_git",
                    version="v1",
                    effect=Effect.READ,
                    description="只读查看工作区 Git 仓库信息（仅限 status、diff、log、show、blame、rev-parse、branch 列表、ls-files）",
                    input_schema={
                        "type": "object",
                        "required": ["subcommand"],
                        "properties": {
                            "subcommand": {"type": "string", "enum": sorted(_ALLOWED_GIT_SUBCOMMANDS)},
                            "args": {"type": "array", "items": {"type": "string", "maxLength": 512}, "maxItems": 20},
                        },
                        "additionalProperties": False,
                    },
                    output_schema={"type": "object"},
                    permissions=frozenset(),
                    timeout=timedelta(seconds=30),
                ),
                self.repo_git,
            ),
        ]

    async def repo_glob(self, invocation: ToolInvocation) -> dict[str, Any]:
        pattern = _validate_glob_pattern(str(invocation.arguments["pattern"]))
        base_rel = str(invocation.arguments.get("path") or ".")
        limit = max(1, min(int(invocation.arguments.get("limit", 200)), 1000))

        matched: list[str] = []
        truncated = False
        if await self.w._archived(invocation):
            env_base = self.w._env_path(base_rel)

            async def _list(box: Any, env_id: str) -> list[str]:
                res = await box.exec(env_id, ["find", env_base, "-type", "f", "-not", "-path", "*/.git/*"])
                if res.exit_code != 0:
                    raise NotFoundError("path_not_found", f"列不出 {base_rel}")
                return res.stdout.decode("utf-8", errors="replace").splitlines()

            raw_paths = await self.w._in_env(invocation, writable=False, run=_list)
            prefix = "" if env_base in (".", "./") else env_base.rstrip("/") + "/"
            for p in sorted(raw_paths):
                clean = p[2:] if p.startswith("./") else p
                if not clean or "/.git/" in clean or clean.startswith(".git/"):
                    continue
                check_rel = clean[len(prefix) :] if prefix and clean.startswith(prefix) else clean
                if _match_glob(check_rel, pattern) or _match_glob(clean, pattern):
                    if len(matched) >= limit:
                        truncated = True
                        break
                    matched.append(clean)
        else:
            root = await self.w._mounted(invocation)
            base_dir = self.w._resolve(root, base_rel)
            if not base_dir.exists() or not base_dir.is_dir():
                raise NotFoundError("path_not_found", f"没有目录 {base_rel}")
            candidates: list[str] = []
            for cur_root, dirs, files in os.walk(base_dir, followlinks=False):
                dirs[:] = sorted(d for d in dirs if d != ".git" and not Path(cur_root, d).is_symlink())
                for fname in sorted(files):
                    fpath = Path(cur_root, fname)
                    if fpath.is_symlink():
                        continue
                    rel_to_root = fpath.relative_to(root).as_posix()
                    rel_to_base = fpath.relative_to(base_dir).as_posix()
                    if _match_glob(rel_to_base, pattern) or _match_glob(rel_to_root, pattern):
                        candidates.append(rel_to_root)
            candidates.sort()
            if len(candidates) > limit:
                truncated = True
                matched = candidates[:limit]
            else:
                matched = candidates

        return {"pattern": pattern, "paths": matched, "total": len(matched), "truncated": truncated}

    async def apply_patch(self, invocation: ToolInvocation) -> dict[str, Any]:
        parsed = _parse_unified_patch(str(invocation.arguments["patch"]))
        # Reject duplicate paths in one patch to keep state deterministic
        seen_paths: set[str] = set()
        for fp in parsed:
            if fp.path in seen_paths:
                raise InvalidError("invalid_patch", f"同一个 patch 中重复出现文件：{fp.path}")
            seen_paths.add(fp.path)

        results: list[dict[str, Any]] = []
        if await self.w._archived(invocation):
            staged_paths = [(fp, self.w._env_path(fp.path)) for fp in parsed]
            if any(rel == ".git" or rel.startswith(".git/") for _, rel in staged_paths):
                raise DeniedError("path_not_allowed", "禁止通过 apply_patch 修改 .git 目录")

            async def _apply_in_env(box: Any, env_id: str) -> None:
                planned: list[tuple[_FilePatch, str, bytes | None]] = []
                for fp, env_rel in staged_paths:
                    old_text: str | None
                    if fp.action == "added":
                        try:
                            await box.read_file(env_id, env_rel)
                            raise InvalidError("patch_conflict", f"文件已存在，无法新增：{fp.path}")
                        except NotFoundError:
                            old_text = None
                    else:
                        try:
                            raw = await box.read_file(env_id, env_rel)
                            old_text = raw.decode("utf-8", errors="strict")
                        except NotFoundError:
                            old_text = None
                        except UnicodeDecodeError:
                            raise InvalidError("binary_file", f"{fp.path} 不是 UTF-8 文本文件") from None
                    new_bytes = _apply_hunks(old_text, fp)
                    planned.append((fp, env_rel, new_bytes))

                for fp, env_rel, new_bytes in planned:
                    if new_bytes is None:
                        await box.exec(env_id, ["rm", "-f", env_rel])
                    else:
                        await box.write_file(env_id, env_rel, new_bytes)
                    results.append({"path": fp.path, "action": fp.action, "hunks": len(fp.hunks)})

            await self.w._in_env(invocation, writable=True, run=_apply_in_env)
        else:
            root = await self.w._mounted(invocation)
            planned_local: list[tuple[_FilePatch, Path, bytes | None]] = []
            for fp in parsed:
                target = self.w._resolve(root, fp.path)
                if target == root:
                    raise InvalidError("invalid_patch", "patch 路径不能指向工作区根目录")
                rel_posix = target.relative_to(root).as_posix()
                if rel_posix == ".git" or rel_posix.startswith(".git/"):
                    raise DeniedError("path_not_allowed", "禁止通过 apply_patch 修改 .git 目录")
                if target.exists():
                    if target.is_dir():
                        raise InvalidError("invalid_patch", f"目标路径是目录：{fp.path}")
                    try:
                        old_text = target.read_text("utf-8")
                    except UnicodeDecodeError:
                        raise InvalidError("binary_file", f"{fp.path} 不是 UTF-8 文本文件") from None
                else:
                    old_text = None
                new_bytes = _apply_hunks(old_text, fp)
                planned_local.append((fp, target, new_bytes))

            async def _commit_local() -> None:
                for fp, target, new_bytes in planned_local:
                    if new_bytes is None:
                        if target.exists():
                            target.unlink()
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(new_bytes)
                    results.append({"path": fp.path, "action": fp.action, "hunks": len(fp.hunks)})

            await self.w._guarded(invocation, root, _commit_local)

        await self.w._changed(invocation)
        return {"files": results, "applied": len(results)}

    async def repo_git(self, invocation: ToolInvocation) -> dict[str, Any]:
        sub = str(invocation.arguments["subcommand"]).strip()
        if sub not in _ALLOWED_GIT_SUBCOMMANDS:
            raise DeniedError("git_subcommand_not_allowed", f"不允许的 git 只读子命令：{sub}")
        extra_args = [str(a) for a in (invocation.arguments.get("args") or [])]
        for arg in extra_args:
            if not arg or "\x00" in arg:
                raise InvalidError("invalid_argument", "git 参数非法")
            head = arg.split("=", 1)[0]
            if head in _FORBIDDEN_GIT_FLAGS:
                raise DeniedError("git_flag_not_allowed", f"禁止使用可能越界或写入的 git 参数：{head}")
            if sub == "branch" and head in _FORBIDDEN_BRANCH_FLAGS:
                raise DeniedError("git_flag_not_allowed", f"只读 repo_git branch 不允许写操作参数：{head}")

        argv = ["git", sub, *extra_args]
        limit = self.w.config.output_limit

        if await self.w._archived(invocation):
            async def _run(box: Any, env_id: str) -> Any:
                return await box.exec(env_id, argv)

            res = await self.w._in_env(invocation, writable=False, run=_run)
            stdout = res.stdout.decode("utf-8", errors="replace")
            stderr = res.stderr.decode("utf-8", errors="replace")
            return {
                "command": " ".join(shlex.quote(a) for a in argv),
                "exit_code": res.exit_code,
                "stdout": stdout[:limit],
                "stderr": stderr[:limit],
                "truncated": len(stdout) > limit or len(stderr) > limit,
            }

        root = await self.w._mounted(invocation)

        def _exec_local() -> tuple[int, bytes, bytes]:
            proc = subprocess.run(
                argv,
                cwd=root,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
                capture_output=True,
                timeout=15,
                check=False,
            )
            return proc.returncode, proc.stdout, proc.stderr

        try:
            code, out_b, err_b = await asyncio.to_thread(_exec_local)
        except (FileNotFoundError, subprocess.TimeoutExpired) as err:
            raise TransientError("git_unavailable", f"执行 git 失败：{err}") from None

        stdout = out_b.decode("utf-8", errors="replace")
        stderr = err_b.decode("utf-8", errors="replace")
        return {
            "command": " ".join(shlex.quote(a) for a in argv),
            "exit_code": code,
            "stdout": stdout[:limit],
            "stderr": stderr[:limit],
            "truncated": len(stdout) > limit or len(stderr) > limit,
        }
