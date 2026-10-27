"""Sandbox（设计书 4.10）：创建环境（资源上限、网络策略）、执行代码、读写文件、销毁。

只在 Python 侧（program 节点）使用。本期只定义接口；开发实现是本地子进程，仅限开发环境，不提供网络隔离。

语义：
- 环境只属于创建它的作用域；别的作用域访问：not_found。销毁后访问：not_found。destroy 重复调用不报错。
- argv[0] 为 spec.runtime 时由实现映射到环境内的解释器，例如 runtime = python 时 ["python", "-c", "..."]。
- exec 超时不抛异常：返回 timed_out=True，exit_code 为 None。代码自身的非零退出码照常返回。
- exec 的 env 是这一条命令额外的环境变量（包镜像地址、出网代理与测试用密钥，D-15、D-16），只对这一条有效；名字要是
  [A-Za-z_][A-Za-z0-9_]*，不能以 AGENT_ 开头，最多 64 个、共 64 KiB，否则 invalid。
- ExecResult.cpu_time 是命令用掉的 CPU 时间（计量用，docs/plan/cloud-deploy.md D-10）；实现量不出来时为 None。
- 文件路径相对于环境的工作目录；绝对路径、含 .. 段或反斜杠：invalid。读不存在的文件：not_found。
- 不同环境的文件互不可见。
- spec.workspace_id 非空时，环境的工作目录就是本租户的这个 workspace（Worker 的工作区类工具读写的同一处，
  <工作区根>/<tenant_id>/<workspace_id>），而不是一个空目录；销毁环境不删 workspace 里的文件。
  工作区根由实现的配置给出，请求改变不了；平台作用域没有 workspace（invalid）；workspace_id 格式同租户 ID，不对时 invalid。
  实现不支持时 invalid（unsupported_workspace）。这是单节点部署的做法（docs/plan/cloud-deploy.md D-05）；
  工作区跟着沙箱走（D-06）要等设计书加上 Workspace 一层。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from agentruntime_core.errors import InvalidError
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
    # 环境所属的会话（会话级沙箱，D-13）：只用来归属与复用（Worker 的 SandboxBroker 按它在沙箱环境表里找），后端不用解释它。
    session_id: str = ""


@dataclass(frozen=True)
class SandboxEnv:
    env_id: str
    spec: SandboxSpec
    # 环境实际用的镜像（后端量得出时按 digest，例如 K8s 的 imageID；D-14），记进沙箱环境表与 trace；量不出时为空。
    image: str = ""


@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    cpu_time: timedelta | None = None


class Sandbox(Protocol):
    async def create(self, spec: SandboxSpec) -> SandboxEnv: ...

    async def exec(
        self, env_id: str, argv: Sequence[str], *, timeout: timedelta, stdin: bytes = b"", env: Mapping[str, str] | None = None
    ) -> ExecResult: ...

    async def write_file(self, env_id: str, path: str, data: bytes) -> None: ...

    async def read_file(self, env_id: str, path: str) -> bytes: ...

    async def destroy(self, env_id: str) -> None: ...


class SandboxProvider(Bindable[Sandbox], Protocol):
    pass


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_EXEC_ENV = 64
MAX_EXEC_ENV_BYTES = 64 * 1024


def check_exec_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """exec 的 env 的规则（各实现共用，与 sandbox-agent 一致）。"""
    if not env:
        return {}
    out = {str(k): str(v) for k, v in env.items()}
    if len(out) > MAX_EXEC_ENV or sum(len(k) + len(v) for k, v in out.items()) > MAX_EXEC_ENV_BYTES:
        raise InvalidError("invalid_env", "env 太多或太大")
    for k, v in out.items():
        if not _ENV_NAME.match(k) or k.upper().startswith("AGENT_") or "\x00" in v:
            raise InvalidError("invalid_env", f"env 的名字 {k} 不合法")
    return out


@dataclass(frozen=True)
class ListedEnvironment:
    """后端里实际在跑的一个环境（对账用，D-11）。owner 是所属作用域（租户 ID 或 _platform）；created_at 是后端记的创建时间，
    对账据此给刚建、还没来得及记进沙箱环境表的环境留出宽限。"""

    env_id: str
    owner: str
    created_at: datetime | None
    state: str = "running"


class SandboxLister(Protocol):
    """能列出本部署在跑的环境的后端（K8s、Cloudflare 桥、开发实现都能）。这是平台级的操作，不绑定租户：
    只列本部署建的环境（K8s 按标签、桥按令牌），别的部署的不碰。"""

    async def list_environments(self) -> list[ListedEnvironment]: ...
