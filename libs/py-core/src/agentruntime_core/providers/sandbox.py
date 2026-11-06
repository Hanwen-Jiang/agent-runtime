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
  实现不支持时 invalid（unsupported_workspace）。这是挂卷的做法（docs/plan/cloud-deploy.md D-05）；不挂卷的后端靠
  下面的 SandboxArchive 把工作区存档与恢复（D-06，docs/plan/workspace-layer.md），Worker 建环境时 workspace_id 置空。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import IO, Literal, Protocol

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
    # 环境所属的会话（会话级沙箱，D-13）：只用来归属与复用（Sandbox Service 按它在沙箱环境表里找，SBX-01），后端不用解释它。
    session_id: str = ""
    # 显式的镜像（按项目定制的运行环境，D-17）：给了就不按 runtime 查登记。后端只接受部署登记的环境镜像仓库下的引用
    # （K8s 的 custom_image_prefix），别的一律 invalid；不支持的后端 invalid（unsupported_image）。
    image: str = ""


@dataclass(frozen=True)
class SandboxEnv:
    env_id: str
    spec: SandboxSpec
    # 环境实际用的镜像（后端量得出时按 digest，例如 K8s 的 imageID；D-14），记进沙箱环境表与 trace；量不出时为空。
    image: str = ""
    # True only after a dedicated backend health check during create; command success is not health.
    # Backends without such a check leave this false (unknown), preserving old implementations.
    health_confirmed: bool = False


@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    cpu_time: timedelta | None = None
    stdout_object: dict[str, str | int] | None = None
    stderr_object: dict[str, str | int] | None = None
    stdout_pending: dict[str, str | int] | None = None
    stderr_pending: dict[str, str | int] | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class ExecNotStopped(asyncio.CancelledError):
    """exec 被取消了，但沙箱没有确认命令已经停下：命令可能还在跑、还在改文件。"""


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


class SandboxHealth(Protocol):
    """Optional dedicated health probe, tenant scoped. Broker calls it only on real reacquisition;
    read-only EnvironmentService queries do not probe, execute commands, or extend lifetime."""

    async def healthy(self, env_id: str) -> bool: ...


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


class SandboxPreview(Protocol):
    """能给环境的一个端口开预览地址的后端（进网，设计书改动稿 W8；D-16）：返回出网网关的预览入口转发到的地址
    （host:port，K8s 是 Pod 的地址，开发实现是本机）。作用域同 exec：别的作用域的环境 not_found。托管后端没有这一项。"""

    async def preview_address(self, env_id: str, port: int) -> str: ...


@dataclass(frozen=True)
class FileEntry:
    """工作目录里的一项（存档的清单，D-06）：path 相对工作目录、/ 分隔；type 是 file、dir、symlink；mode 只留权限位
    （符号链接为 0）；文件有 size 与 sha256（十六进制），符号链接有 link（相对的目标）。"""

    path: str
    type: str
    mode: int
    size: int = 0
    sha256: str = ""
    link: str = ""
    chunks: tuple[tuple[str, int], ...] = ()

    @property
    def blocks(self) -> tuple[tuple[str, int], ...]:
        return (self.chunks or ((self.sha256, self.size),)) if self.type == "file" else ()

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"path": self.path, "type": self.type, "mode": self.mode}
        if self.type == "file":
            out["size"], out["sha256"] = self.size, self.sha256
            if self.chunks:
                out["chunks"] = [{"sha256": sha, "size_bytes": size} for sha, size in self.chunks]
        if self.type == "symlink":
            out["link"] = self.link
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> FileEntry:
        def number(key: str) -> int:
            value = data.get(key) or 0
            return int(value) if isinstance(value, (int, float, str)) else 0

        return cls(
            path=str(data["path"]), type=str(data["type"]), mode=number("mode"), size=number("size"),
            sha256=str(data.get("sha256") or ""), link=str(data.get("link") or ""),
            chunks=tuple((str(b["sha256"]), int(b["size_bytes"])) for b in (data.get("chunks") or ())),
        )


@dataclass(frozen=True)
class DirEntry:
    name: str
    type: str
    size: int = 0


@dataclass(frozen=True)
class GrepMatch:
    path: str
    line: int
    text: str


class SandboxArchive(Protocol):
    """能整体导出、导入工作目录的后端（工作区的存档模式，设计书改动稿 W2；D-06）。作用域同 exec：别的作用域的环境 not_found。

    - export_archive：工作目录打成 tar 写进 out（普通文件、目录与不出工作目录的符号链接，只留权限位；设备、管道与指出去的
      符号链接不进），返回写了多少字节；文件总大小超过 max_bytes 时 invalid（too_large）。
    - import_archive：把 tar 解到工作目录；工作目录要是空的（invalid，not_empty）；绝对路径、..、反斜杠、硬链接与设备文件、
      指出工作目录的符号链接、经符号链接写进去的路径、重名的项一律 invalid；超过 max_bytes invalid（too_large）；后端建不了
      符号链接时 invalid（symlink_unsupported）。目录的权限位至少是 0o700（解开时要能往里写）。
    - manifest：逐项的清单（FileEntry，按路径的字符串顺序排序）。
    """

    async def export_archive(self, env_id: str, out: IO[bytes], *, max_bytes: int) -> int: ...

    async def import_archive(self, env_id: str, src: IO[bytes], *, size: int, max_bytes: int) -> None: ...

    async def manifest(self, env_id: str) -> list[FileEntry]: ...


class SandboxBrowse(Protocol):
    """能在环境里列目录、按行搜索的后端（工作区类工具在承载它的环境里执行，D-06）。path 相对工作目录，空为工作目录本身；
    列目录最多 1000 项、按名字排序，目录不存在 not_found。搜索按行匹配正则（sandbox-agent 是 RE2，开发实现是 Python 的 re），
    跳过任何一级 .git 目录、二进制（前 1 KiB 有 NUL）与超过 1 MiB 的文件，每行最多 500 个字符，最多 max_matches 条
    （上限 1000），返回（匹配，是否截断）；正则不合法 invalid。"""

    async def list_dir(self, env_id: str, path: str = "") -> list[DirEntry]: ...

    async def grep(self, env_id: str, pattern: str, *, path: str = "", max_matches: int = 100) -> tuple[list[GrepMatch], bool]: ...
