"""本地子进程 Sandbox（开发实现，仅限开发环境）。语义见 agentruntime_core.providers.sandbox。

每个环境一个工作目录：<root>/<作用域>/<env_id>/work；spec.workspace_id 非空时是 <workspaces>/<tenant_id>/<workspace_id>
（给了 workspaces 才支持）。不提供网络隔离与资源上限，不能运行不可信代码。
argv[0] 为 python 时换成当前解释器。子进程只拿 _ENV 白名单里的环境变量（与平台内工具 shell_run 相同）：
Worker 进程的密钥与 AR_* 配置不传给沙箱里的代码（设计书 4.12：节点代码永远拿不到长期密钥）。
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentruntime_core.dev._common import scope_id
from agentruntime_core.errors import InvalidError, NotFoundError
from agentruntime_core.providers.sandbox import ExecResult, ListedEnvironment, SandboxEnv, SandboxSpec, check_exec_env
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector

_DRIVE = re.compile(r"^[A-Za-z]:")
_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "LANG", "LC_ALL")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


@dataclass
class _Env:
    owner: str
    spec: SandboxSpec
    workdir: Path
    destroyed: bool = False
    # 绑定 workspace 的环境销毁时只删记录，不删目录。
    owns_workdir: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _relative(path: str) -> Path:
    if not isinstance(path, str) or not path or "\\" in path or path.startswith("/") or _DRIVE.match(path):
        raise InvalidError("invalid_path", "路径必须是工作目录内的相对路径")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise InvalidError("invalid_path", "路径不得含空段、. 或 ..")
    return Path(*parts)


class LocalSubprocessSandboxProvider:
    def __init__(self, root: Path, *, workspaces: Path | None = None, faults: FaultInjector | None = None) -> None:
        self._root = Path(root)
        self._workspaces = Path(workspaces) if workspaces is not None else None
        self._faults = faults or FaultInjector()
        self._envs: dict[str, _Env] = {}

    def bind(self, tenant: TenantContext) -> LocalSubprocessSandbox:
        return LocalSubprocessSandbox(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> LocalSubprocessSandbox:
        return LocalSubprocessSandbox(self, platform_scope(purpose))

    async def aclose(self) -> None:
        self._envs.clear()

    async def list_environments(self) -> list[ListedEnvironment]:
        """本进程建的、还没销毁的环境（开发实现的环境只在进程内）。"""
        return [ListedEnvironment(env_id, env.owner, env.created_at) for env_id, env in sorted(self._envs.items()) if not env.destroyed]


class LocalSubprocessSandbox:
    def __init__(self, provider: LocalSubprocessSandboxProvider, scope: Scope) -> None:
        self._provider = provider
        self._scope = scope
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
        if spec.image:
            raise InvalidError("unsupported_image", "开发实现不用镜像")
        env_id = "env-" + secrets.token_hex(8)
        if spec.workspace_id:
            workdir = workspace_dir(self._provider._workspaces, self._scope, spec.workspace_id)
            workdir.mkdir(parents=True, exist_ok=True)
            self._provider._envs[env_id] = _Env(owner=self._owner, spec=spec, workdir=workdir, owns_workdir=False)
            return SandboxEnv(env_id=env_id, spec=spec)
        workdir = self._provider._root / self._owner / env_id / "work"
        workdir.mkdir(parents=True)
        self._provider._envs[env_id] = _Env(owner=self._owner, spec=spec, workdir=workdir)
        return SandboxEnv(env_id=env_id, spec=spec)

    async def exec(
        self, env_id: str, argv: Sequence[str], *, timeout: timedelta, stdin: bytes = b"", env: Mapping[str, str] | None = None
    ) -> ExecResult:
        self._provider._faults.check("exec")
        extra = check_exec_env(env)
        box = self._env(env_id)
        if not argv:
            raise InvalidError("invalid_argument", "argv 不能为空")
        command = [sys.executable if argv[0] == box.spec.runtime else argv[0], *argv[1:]]
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=box.workdir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**{k: os.environ[k] for k in _ENV if k in os.environ}, "PYTHONIOENCODING": "utf-8", **extra},
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

    async def preview_address(self, env_id: str, port: int) -> str:
        """预览地址转发到本机的这个端口（开发实现的命令就在本机跑；D-16）。"""
        self._env(env_id)
        if not 1 <= int(port) <= 65535:
            raise InvalidError("invalid_port", "端口要在 1 到 65535 之间")
        return f"127.0.0.1:{int(port)}"

    async def destroy(self, env_id: str) -> None:
        self._provider._faults.check("destroy")
        env = self._env(env_id, allow_destroyed=True)
        if not env.destroyed:
            env.destroyed = True
            if env.owns_workdir:
                shutil.rmtree(env.workdir.parent, ignore_errors=True)


def workspace_subpath(scope: Scope, workspace_id: str) -> str:
    """绑定 workspace 时相对工作区根的路径 <tenant_id>/<workspace_id>。各实现共用同一套校验。"""
    if scope.is_platform:
        raise InvalidError("unsupported_workspace", "平台作用域没有 workspace")
    if not isinstance(workspace_id, str) or not _SLUG.match(workspace_id):
        raise InvalidError("invalid_workspace", "workspace_id 的格式不对")
    return f"{scope.tenant_id}/{workspace_id}"


def workspace_dir(root: Path | None, scope: Scope, workspace_id: str) -> Path:
    """绑定 workspace 时的工作目录：<root>/<tenant_id>/<workspace_id>。"""
    if root is None:
        raise InvalidError("unsupported_workspace", "这个 Sandbox 没有配置工作区根，不能绑定 workspace")
    return Path(root) / workspace_subpath(scope, workspace_id)
