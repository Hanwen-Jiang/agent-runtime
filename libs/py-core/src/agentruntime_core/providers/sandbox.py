"""Sandbox（设计书 4.10）：创建环境（资源上限、网络策略）、执行代码、读写文件、销毁。

只在 Python 侧（program 节点）使用。本期只定义接口；开发实现是本地子进程，仅限开发环境，不提供网络隔离。

语义：
- 环境只属于创建它的作用域；别的作用域访问：not_found。销毁后访问：not_found。destroy 重复调用不报错。
- argv[0] 为 spec.runtime 时由实现映射到环境内的解释器，例如 runtime = python 时 ["python", "-c", "..."]。
- exec 超时不抛异常：返回 timed_out=True，exit_code 为 None。代码自身的非零退出码照常返回。
- 文件路径相对于环境的工作目录；绝对路径、含 .. 段或反斜杠：invalid。读不存在的文件：not_found。
- 不同环境的文件互不可见。
- spec.workspace_id 非空时，环境的工作目录就是本租户的这个 workspace（Worker 的工作区类工具读写的同一处，
  <工作区根>/<tenant_id>/<workspace_id>），而不是一个空目录；销毁环境不删 workspace 里的文件。
  工作区根由实现的配置给出，请求改变不了；平台作用域没有 workspace（invalid）；workspace_id 格式同租户 ID，不对时 invalid。
  实现不支持时 invalid（unsupported_workspace）。这是单节点部署的做法（docs/plan/cloud-deploy.md D-05）；
  工作区跟着沙箱走（D-06）要等设计书加上 Workspace 一层。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, Protocol

from agentruntime_core.providers.base import Bindable


@dataclass(frozen=True)
class SandboxSpec:
    runtime: str = "python"
    cpu_millis: int = 1000
    memory_mb: int = 512
    network: Literal["none", "allowlist"] = "none"
    allow_hosts: tuple[str, ...] = ()
    idle_timeout: timedelta = timedelta(minutes=10)
    workspace_id: str = ""


@dataclass(frozen=True)
class SandboxEnv:
    env_id: str
    spec: SandboxSpec


@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool


class Sandbox(Protocol):
    async def create(self, spec: SandboxSpec) -> SandboxEnv: ...

    async def exec(
        self, env_id: str, argv: Sequence[str], *, timeout: timedelta, stdin: bytes = b""
    ) -> ExecResult: ...

    async def write_file(self, env_id: str, path: str, data: bytes) -> None: ...

    async def read_file(self, env_id: str, path: str) -> bytes: ...

    async def destroy(self, env_id: str) -> None: ...


class SandboxProvider(Bindable[Sandbox], Protocol):
    pass
