"""本地子进程 Sandbox（开发实现，仅限开发环境）。语义见 agentruntime_core.providers.sandbox。

每个环境一个工作目录：<root>/<作用域>/<env_id>/work。不提供网络隔离与资源上限，不能运行不可信代码。
argv[0] 为 python 时换成当前解释器。
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from agentruntime_core.dev._common import scope_id
from agentruntime_core.errors import InvalidError, NotFoundError
from agentruntime_core.providers.sandbox import ExecResult, SandboxEnv, SandboxSpec
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector

_DRIVE = re.compile(r"^[A-Za-z]:")


@dataclass
class _Env:
    owner: str
    spec: SandboxSpec
    workdir: Path
    destroyed: bool = False


def _relative(path: str) -> Path:
    if not isinstance(path, str) or not path or "\\" in path or path.startswith("/") or _DRIVE.match(path):
        raise InvalidError("invalid_path", "路径必须是工作目录内的相对路径")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise InvalidError("invalid_path", "路径不得含空段、. 或 ..")
    return Path(*parts)


class LocalSubprocessSandboxProvider:
    def __init__(self, root: Path, *, faults: FaultInjector | None = None) -> None:
        self._root = Path(root)
        self._faults = faults or FaultInjector()
        self._envs: dict[str, _Env] = {}

    def bind(self, tenant: TenantContext) -> LocalSubprocessSandbox:
        return LocalSubprocessSandbox(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> LocalSubprocessSandbox:
        return LocalSubprocessSandbox(self, platform_scope(purpose))

    async def aclose(self) -> None:
        self._envs.clear()


class LocalSubprocessSandbox:
    def __init__(self, provider: LocalSubprocessSandboxProvider, scope: Scope) -> None:
        self._provider = provider
        self._owner = scope_id(scope)

    def _env(self, env_id: str, *, allow_destroyed: bool = False) -> _Env:
        env = self._provider._envs.get(env_id)
        if env is None or env.owner != self._owner or (env.destroyed and not allow_destroyed):
            raise NotFoundError("unknown_env", "环境不存在")
        return env

    async def create(self, spec: SandboxSpec) -> SandboxEnv:
        self._provider._faults.check("create")
        if spec.runtime != "python":
            raise InvalidError("unsupported_runtime", "开发实现只支持 python")
        env_id = "env-" + secrets.token_hex(8)
        workdir = self._provider._root / self._owner / env_id / "work"
        workdir.mkdir(parents=True)
        self._provider._envs[env_id] = _Env(owner=self._owner, spec=spec, workdir=workdir)
        return SandboxEnv(env_id=env_id, spec=spec)

    async def exec(self, env_id: str, argv: Sequence[str], *, timeout: timedelta, stdin: bytes = b"") -> ExecResult:
        self._provider._faults.check("exec")
        env = self._env(env_id)
        if not argv:
            raise InvalidError("invalid_argument", "argv 不能为空")
        command = [sys.executable if argv[0] == env.spec.runtime else argv[0], *argv[1:]]
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=env.workdir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=timeout.total_seconds())
        except TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            return ExecResult(exit_code=None, stdout=stdout, stderr=stderr, timed_out=True)
        return ExecResult(exit_code=process.returncode, stdout=stdout, stderr=stderr, timed_out=False)

    async def write_file(self, env_id: str, path: str, data: bytes) -> None:
        self._provider._faults.check("write_file")
        env = self._env(env_id)
        target = env.workdir / _relative(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def read_file(self, env_id: str, path: str) -> bytes:
        self._provider._faults.check("read_file")
        env = self._env(env_id)
        target = env.workdir / _relative(path)
        if not target.is_file():
            raise NotFoundError("not_found", "文件不存在")
        return target.read_bytes()

    async def destroy(self, env_id: str) -> None:
        self._provider._faults.check("destroy")
        env = self._env(env_id, allow_destroyed=True)
        if not env.destroyed:
            env.destroyed = True
            shutil.rmtree(env.workdir.parent, ignore_errors=True)
