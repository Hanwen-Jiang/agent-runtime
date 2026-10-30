"""TMP 计划 eval-observability-and-runtime-fixes 第 11.10 节（S-01 草案，D-06 的后续）：克隆到一个提交，并在克隆之后写入文件。

SWE-bench 的每道题要停在 base_commit，还要放进题目的运行环境定义：

1. ref 是完整的提交（40 位十六进制）时，工作区停在这个提交上，HEAD 分离（branch 为空）。
2. 只取到这个提交为止的历史：之后的提交（题目的修复）不在任何引用里，对象也没有；远端的分支与标签都不取。
3. 与 repo_url 一起给的 files 在克隆之后写入、另做一次提交：head 是这次提交，它的父提交是 base；相对 head 的 diff 只含之后的改动。
4. 不在远端的提交报错，并撤掉这一行（同名可以再建）。
存档与挂卷两种后端各跑一遍。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentruntime.common.v1 import error_pb2

from .test_workspace_operations import Op, Setup, git

ENV_FILES = [
    {"path": ".devcontainer/devcontainer.json", "content": '{"build": {"dockerfile": "agentruntime.Dockerfile"}}\n'},
    {"path": ".devcontainer/agentruntime.Dockerfile", "content": "FROM python:3.13-slim\n"},
]


def remote_with_history(tmp_path: Path) -> tuple[str, Path, str, str]:
    """裸仓库：base（题目报的那个提交）之后还有一个修复提交与一个标签。返回 (地址, 裸仓库, base, 修复)。"""
    bare = tmp_path / "remote.git"
    git("init", "--quiet", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git("init", "--quiet", "-b", "main", str(seed))
    (seed / "calc.py").write_bytes(b"def add(a, b):\n    return a - b\n")
    git("add", "-A", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--quiet", "-m", "base", cwd=seed)
    base = git("rev-parse", "HEAD", cwd=seed)
    (seed / "calc.py").write_bytes(b"def add(a, b):\n    return a + b\n")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--quiet", "-am", "fix add", cwd=seed)
    fix = git("rev-parse", "HEAD", cwd=seed)
    git("tag", "v1.0", cwd=seed)
    git("push", "--quiet", "--tags", str(bare), "main", cwd=seed)
    return bare.as_uri(), bare, base, fix


async def sh(s: Setup, *argv: str) -> dict:
    return await s.tool("shell_run", {"argv": ["git", "-c", "safe.directory=*", *argv]})


@pytest.mark.parametrize("mode", ["archive", "mount"])
async def test_clone_at_a_commit_with_files(tmp_path: Path, mode: str) -> None:
    url, _, base, fix = remote_with_history(tmp_path)
    s = Setup(tmp_path, mode=mode)
    created = (await s.run(Op.WORKSPACE_OPERATION_CREATE, repo_url=url, ref=base, files=ENV_FILES))["workspace"]
    head = created["head"]
    assert head not in (base, fix), "head 是写入文件的那次提交"
    assert created.get("branch", "") == "", "停在提交上时 HEAD 是分离的"

    assert (await sh(s, "rev-parse", "HEAD^"))["stdout"].strip() == base, "写入文件的提交的父提交是 base"
    assert (await sh(s, "log", "-1", "--format=%s"))["stdout"].strip() == "Add workspace files"
    assert (await s.tool("repo_read", {"path": "calc.py"}))["content"] == "def add(a, b):\n    return a - b\n", "文件是 base 的样子"
    assert (await s.tool("repo_read", {"path": ".devcontainer/agentruntime.Dockerfile"}))["content"] == "FROM python:3.13-slim\n"

    every = (await sh(s, "log", "--all", "--format=%H"))["stdout"].split()
    assert set(every) == {head, base}, f"只有 base 与它之前的历史，加上写入文件的提交：{every}"
    assert (await sh(s, "cat-file", "-e", fix))["exit_code"] != 0, "修复提交的对象也不在工作区里"
    assert (await s.error(Op.WORKSPACE_OPERATION_DIFF, base=fix)).category == error_pb2.ERROR_CATEGORY_NOT_FOUND, "相对它取 diff：NotFound"
    assert (await sh(s, "tag", "--list"))["stdout"].strip() == "", "远端的标签不取"
    assert (await sh(s, "for-each-ref", "--format=%(refname)"))["stdout"].strip() == "", "没有任何引用：远端的分支与标签都不取"

    assert (await s.run(Op.WORKSPACE_OPERATION_DIFF, base=head)).get("files", []) == [], "刚建好时相对 head 没有改动"
    await s.tool("repo_edit", {"path": "calc.py", "old": "a - b", "new": "a + b"})
    diff = await s.run(Op.WORKSPACE_OPERATION_DIFF, base=head)
    assert diff["files"] == ["calc.py"], "补丁只含之后的改动，不含写入的文件"
    assert "+    return a + b" in diff["diff"] and ".devcontainer" not in diff["diff"]


async def test_clone_at_a_commit_without_files(tmp_path: Path) -> None:
    url, _, base, _ = remote_with_history(tmp_path)
    s = Setup(tmp_path, mode="archive")
    created = (await s.run(Op.WORKSPACE_OPERATION_CREATE, repo_url=url, ref=base))["workspace"]
    assert created["head"] == base and created.get("branch", "") == ""


async def test_a_branch_ref_still_clones_the_branch(tmp_path: Path) -> None:
    url, bare, _, _ = remote_with_history(tmp_path)
    s = Setup(tmp_path, mode="archive")
    created = (await s.run(Op.WORKSPACE_OPERATION_CREATE, repo_url=url, ref="main"))["workspace"]
    assert created["head"] == git("rev-parse", "main", cwd=bare) and created["branch"] == "main", "分支照旧"


async def test_a_commit_that_is_not_on_the_remote(tmp_path: Path) -> None:
    url, _, _, _ = remote_with_history(tmp_path)
    s = Setup(tmp_path, mode="archive")
    missing = "0123456789abcdef" * 2 + "01234567"
    err = await s.error(Op.WORKSPACE_OPERATION_CREATE, repo_url=url, ref=missing)
    assert err.category == error_pb2.ERROR_CATEGORY_NOT_FOUND, err
    assert (await s.ops.run(s.inp(Op.WORKSPACE_OPERATION_CREATE, repo_url=url))).HasField("result"), "没建成时撤掉了这一行，同名可以再建"
