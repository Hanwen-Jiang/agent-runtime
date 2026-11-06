"""Exercise repository scoping with real Git even in a socket-restricted runner.

Only the transport is local: the production lease validation and Git command
construction run unchanged against two on-disk repositories.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentruntime.common.v1 import tenant_pb2
from agentruntime.workflow.v1 import workspace_pb2
from agentruntime_core.errors import InvalidError
from agentruntime_core.providers.sandbox import ExecResult
from agentruntime_worker.workspace import multirepo
from agentruntime_worker.workspace.operations import WorkspaceOperations


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-c", "safe.directory=*", "-c", "user.name=test", "-c", "user.email=test@example.test", *args], cwd=root, check=True, capture_output=True, text=True)
    return result.stdout.strip()


class LocalTransport:
    def __init__(self, root: Path):
        self.root = root
        self.commands = 0

    async def list_dir(self, env_id: str, path: str):
        return [SimpleNamespace(name=p.name, type="symlink" if p.is_symlink() else "dir" if p.is_dir() else "file") for p in (self.root / path).iterdir()]

    async def exec(self, env_id: str, argv, *, timeout: timedelta, stdin=b"", env=None):
        self.commands += 1
        result = subprocess.run(argv, cwd=self.root, input=stdin, env={**os.environ, **(env or {})}, timeout=timeout.total_seconds(), capture_output=True)
        return ExecResult(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr, timed_out=False)


def setup(tmp_path: Path):
    for name in ("a", "b"):
        path = tmp_path / name
        path.mkdir()
        git(path, "init", "-q", "-b", "main")
        (path / "tracked.txt").write_text(name)
        git(path, "add", "tracked.txt")
        git(path, "commit", "-qm", name)
    transport = LocalTransport(tmp_path)
    lease = SimpleNamespace(box=transport, env_id="local", env={}, archive=False, backend=SimpleNamespace(kind="local", name="local"))

    async def acquire(*args, **kwargs):
        return lease

    async def release(*args, **kwargs):
        pass

    async def secret():
        return None

    operations = WorkspaceOperations(SimpleNamespace(acquire=acquire, release=release), secret=secret)
    inp = workspace_pb2.WorkspaceOperationInput(tenant=tenant_pb2.TenantContext(tenant_id="t1", user_id="alice"), workspace_id="multi", operation_id="test", requested_by="alice")
    return operations, inp, transport


async def test_scoped_git_ignores_another_worktree_in_local_config(tmp_path: Path):
    operations, inp, _ = setup(tmp_path)
    git(tmp_path / "a", "config", "core.worktree", "../../b")
    (tmp_path / "b" / "private.txt").write_text("must survive")
    (tmp_path / "a" / "discard.txt").write_text("discard only this")
    before = git(tmp_path / "b", "rev-parse", "HEAD")
    token = multirepo.selected.set({"name": "a", "path": "a"})
    try:
        async with operations._lease(inp, writable=True) as lease:
            await operations._git(lease, ["reset", "--hard", "HEAD"], what="reset a")
            await operations._git(lease, ["clean", "-fd"], what="clean a")
            # Credential-bearing remote commands still use their own safe bare repo.
            async with operations._detached(lease) as detached:
                result = await operations._remote_git(lease, detached, ["ls-remote", "--", (tmp_path / "b").as_uri(), "refs/heads/main"], what="read remote")
                assert before.encode() in result.stdout
    finally:
        multirepo.selected.reset(token)
    assert (tmp_path / "b" / "private.txt").read_text() == "must survive"
    assert not (tmp_path / "a" / "discard.txt").exists()
    assert git(tmp_path / "b", "rev-parse", "HEAD") == before


@pytest.mark.parametrize("kind", ["symlink", "gitfile"])
async def test_git_directory_cannot_point_at_another_repository(tmp_path: Path, kind: str):
    operations, inp, transport = setup(tmp_path)
    shutil.rmtree(tmp_path / "a" / ".git")
    if kind == "symlink":
        (tmp_path / "a" / ".git").symlink_to("../b/.git", target_is_directory=True)
    else:
        (tmp_path / "a" / ".git").write_text("gitdir: ../b/.git\n")
    before = git(tmp_path / "b", "rev-parse", "HEAD")
    token = multirepo.selected.set({"name": "a", "path": "a"})
    try:
        with pytest.raises(InvalidError, match="invalid_repo_path"):
            async with operations._lease(inp, writable=True):
                pytest.fail("linked repository must not be admitted")
    finally:
        multirepo.selected.reset(token)
    assert transport.commands == 0
    assert git(tmp_path / "b", "rev-parse", "HEAD") == before
