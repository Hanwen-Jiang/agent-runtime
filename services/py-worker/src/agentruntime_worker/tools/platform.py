"""平台内工具（设计书 4.9；T-10，细化见 docs/plan/tool-driver.md 第 2 节第 4 条、第 3 节）。

登记进开发用的 LocalToolDriverProvider（平台作用域，所有租户可见），经 ToolDriver 的契约调用：参数按 input_schema 校验、
写操作要幂等键、幂等记录、超时都由 LocalToolDriver 负责，这里只写工具本身。

- 工作区类（配了 workspace_root，或登记了存档模式的沙箱后端时登记）：repo_read、repo_list、repo_grep（read）、repo_edit、
  shell_run（write_reversible）。工作区是 <workspace_root>/<tenant_id>/<workspace_id，没有为 default>，租户与 workspace 来自
  调用的作用域（ToolInvocation），参数改变不了它。路径一律相对工作区；绝对路径、盘符、..、反斜杠、规范化后出界（含经符号链接）
  都拒绝。租户的沙箱后端是存档模式（workspace: archive，D-06）时，这几个工具都在承载工作区的环境里执行（经 SandboxBroker 借：
  写的借可写的环境，读的先找本会话可写的环境、没有时借只读的），Worker 的工作区目录不碰。
- shell_run：argv 列表、不经 shell，在工作区里执行；子进程的环境只留 PATH 等必需的变量，Worker 的其他环境变量（含密钥）
  带不进去；输出截断；超时杀掉进程并返回 timed_out，不抛异常；非零退出码照常返回。shell_sandbox = host 时直接在宿主机上跑
  （开发实现，没有资源限制与网络隔离）；= docker 时在一次性容器里跑：只挂载本工作区到 /workspace，不联网，限 CPU、内存
  与进程数，跑完删除容器（为 coding agent 加的，见 coding-agent 仓库的 docs/plan/coding-agent-mvp.md）；= sandbox 时经
  Sandbox 接口（设计书 4.10）：按租户策略选登记的后端（K8s 的 gVisor、Kata，Cloudflare），每条命令建一个绑定本 workspace、
  断网的环境，跑完销毁（tools/sandboxes.py；docs/plan/cloud-deploy.md D-05）。
- web_fetch（配了 fetch_hosts 才登记）：只认 http、https；只去名单里的主机（"*" 任何主机，"*.example.com" 子域名）；
  解析出的地址是内网、回环、链路本地等一律拒绝（allow_private_fetch 只给测试用）；不走代理、不跟随重定向（返回状态码与
  Location）；正文有大小上限。
- request_network（shell_sandbox = sandbox、有沙箱环境表，且配了出网网关或登记了托管后端时登记；tools/network.py，D-16）：
  write_irreversible，运行中申请联网授权，经人工审批。
- context_read（给了 Context Builder 的 ObjectStore 就登记，不要别的配置）：按 obj:// 路径取回本租户 context/ 与
  node-results/ 下的对象（Context Builder 外置或遮蔽的内容、Node 的外置结果），长内容按 offset 分段（R-03 评审 D11）。
  别的租户的路径一律 not_found，别的前缀 denied。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import os
import re
import shutil
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.enums import Effect
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError, ProviderError, TransientError
from agentruntime_core.providers.object_store import ObjectStoreProvider
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.providers.telemetry import TelemetryProvider
from agentruntime_core.providers.tool_driver import ToolHandler, ToolInvocation, ToolSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.broker import SandboxBroker
from agentruntime_worker.sandbox.environments import Resolution, workspace_reader
from agentruntime_worker.tools.sandboxes import Sandboxes, parse_cpu_millis, parse_memory_mb

VERSION = "v1"
CONTEXT_READ = "context_read"
_CONTEXT_PREFIXES = ("context/", "node-results/")
# 子进程只带这些环境变量（存在时）：能找到程序、在 Windows 上能启动 Python，别的一概不带。
_SHELL_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "LANG", "LC_ALL")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml")


@dataclass(frozen=True)
class PlatformToolsConfig:
    # 工作区根；为 None 时不登记工作区类工具（不会退回到当前目录）。
    workspace_root: Path | None = None
    # web_fetch 的主机名单；为空时不登记 web_fetch。
    fetch_hosts: tuple[str, ...] = ()
    # 允许访问内网地址：只给测试用（本机起的 HTTP 服务）。
    allow_private_fetch: bool = False
    shell_timeout_s: int = 120
    max_shell_timeout_s: int = 600
    # shell_run 在哪里跑：host、docker 或 sandbox；docker 时的镜像、docker 可执行文件与资源上限（sandbox 时 CPU 与内存也取这里）。
    shell_sandbox: str = "host"
    shell_image: str = "python:3.13-slim"
    shell_docker: str = "docker"
    shell_cpus: str = "2"
    shell_memory: str = "2g"
    shell_pids: int = 512
    # shell_sandbox = sandbox 时按（工作区，会话）复用环境（D-13）；要有沙箱环境表（业务库）才生效。
    session_sandboxes: bool = False
    # shell_sandbox = sandbox 时用哪个运行环境（D-14）：后端登记把它映射到镜像，例如 universal（多语言）或 python。
    shell_runtime: str = "python"
    # 登记 request_network（D-16）：配了出网网关时打开；登记了托管后端（按域名放行）时也登记。预览地址的域名。
    network_requests: bool = False
    preview_domain: str = ""
    # shell_run 的 stdout、stderr 与 repo_read 的内容各自最多这么多字符。
    output_limit: int = 64 * 1024
    fetch_limit: int = 1024 * 1024
    fetch_timeout_s: float = 20.0
    max_read_lines: int = 2000
    # context_read 不写 max_chars 时一次取多少个字符（上限是 output_limit）。
    context_read_chars: int = 16_000


def _spec(name: str, effect: Effect, description: str, properties: dict[str, Any], required: list[str], timeout_s: float) -> ToolSpec:
    schema = {"type": "object", "required": required, "properties": properties, "additionalProperties": False}
    return ToolSpec(
        name=name,
        version=VERSION,
        description=description,
        input_schema=schema,
        output_schema={"type": "object"},
        effect=effect,
        permissions=frozenset(),
        timeout=timedelta(seconds=timeout_s),
    )


_PATH = {"type": "string", "minLength": 1, "description": "相对工作区的路径，用 / 分隔"}


def register_platform_tools(
    provider: LocalToolDriverProvider,
    config: PlatformToolsConfig,
    *,
    objects: ObjectStoreProvider | None = None,
    sandboxes: Sandboxes | SandboxBroker | None = None,
    telemetry: TelemetryProvider | None = None,
) -> list[str]:
    """按配置登记平台内工具，返回登记了的名字。给了 objects（Context Builder 外置内容的 ObjectStore）就登记 context_read；
    shell_sandbox = sandbox 时 shell_run 用 sandboxes 里的后端，每次运行记一个 runtime.sandbox span（给了 telemetry 时）。"""
    if config.shell_sandbox == "sandbox" and config.workspace_root is not None and sandboxes is None:
        raise ValueError("shell_sandbox = sandbox 时要给沙箱后端（AR_SANDBOX_BACKENDS_FILE）")
    tools: list[tuple[ToolSpec, ToolHandler]] = []
    broker = SandboxBroker(sandboxes) if isinstance(sandboxes, Sandboxes) else sandboxes
    # 存档模式的后端（D-06）不用 Worker 的工作区目录：只登记了这类后端时不配 workspace_root 也有工作区类工具。
    archived = config.shell_sandbox == "sandbox" and broker is not None and broker.sandboxes.has_archive_backends
    if config.workspace_root is not None or archived:
        root = Path(config.workspace_root) if config.workspace_root is not None else None
        tools += _Workspaces(root, config, broker, telemetry).tools()
    if config.shell_sandbox == "sandbox" and broker is not None and broker.grants is not None:
        hosted = any(broker.sandboxes.spec(n).get("kind") not in ("k8s", "local") for n in broker.sandboxes.names)
        if config.network_requests or hosted:
            from agentruntime_worker.tools.network import NetworkTool

            tools.append(NetworkTool(
                broker, preview_domain=config.preview_domain, runtime=config.shell_runtime, session_sandboxes=config.session_sandboxes,
            ).tool())
    if config.fetch_hosts:
        tools.append(_Fetcher(config).tool())
    if objects is not None:
        tools.append(_ContextReader(objects, config).tool())
    for spec, handler in tools:
        provider.register(spec, handler)
    return [spec.name for spec, _ in tools]


# --- 工作区 ---


class _Workspaces:
    def __init__(
        self, root: Path | None, config: PlatformToolsConfig, broker: SandboxBroker | None = None, telemetry: TelemetryProvider | None = None
    ) -> None:
        self.root = root
        self.config = config
        self.broker = broker
        self.telemetry = telemetry

    def tools(self) -> list[tuple[ToolSpec, ToolHandler]]:
        c = self.config
        return [
            (
                _spec("repo_read", Effect.READ, "读工作区里的文本文件；可以用 start_line、max_lines 只取一段", {
                    "path": _PATH,
                    "start_line": {"type": "integer", "minimum": 1, "description": "从第几行开始（从 1 起）"},
                    "max_lines": {"type": "integer", "minimum": 1, "maximum": c.max_read_lines, "description": "最多取多少行"},
                }, ["path"], 30),
                self.read,
            ),
            (
                _spec("repo_list", Effect.READ, "列出工作区里一个目录的内容", {"path": {**_PATH, "description": "目录；不写为工作区根"}}, [], 30),
                self.list,
            ),
            (
                _spec("repo_grep", Effect.READ, "在工作区的文件里按正则搜索，返回文件、行号与那一行", {
                    "pattern": {"type": "string", "minLength": 1, "description": "Python 正则表达式"},
                    "path": {**_PATH, "description": "只在这个目录或文件里搜；不写为整个工作区"},
                    "max_matches": {"type": "integer", "minimum": 1, "maximum": 1000},
                }, ["pattern"], 60),
                self.grep,
            ),
            (
                _spec("repo_edit", Effect.WRITE_REVERSIBLE, "改工作区里的文件：给了 old 就把它唯一的一处替换成 new，否则用 new 写整个文件（可新建）", {
                    "path": _PATH,
                    "old": {"type": "string", "minLength": 1, "description": "要替换的原文，必须在文件里恰好出现一次"},
                    "new": {"type": "string", "description": "替换后的文字，或整个文件的新内容"},
                }, ["path", "new"], 30),
                self.edit,
            ),
            (
                _spec("shell_run", Effect.WRITE_REVERSIBLE, "在工作区里执行一条命令（argv 列表，不经 shell），返回退出码与输出；可能改动工作区", {
                    "argv": {"type": "array", "minItems": 1, "items": {"type": "string"}, "description": "程序与参数，例如 [\"python\", \"-m\", \"pytest\", \"-q\"]"},
                    "timeout_s": {"type": "integer", "minimum": 1, "maximum": c.max_shell_timeout_s},
                }, ["argv"], c.max_shell_timeout_s + 30),
                self.shell,
            ),
        ]

    # 路径

    @staticmethod
    def _workspace_id(invocation: ToolInvocation) -> str:
        tenant, workspace = invocation.tenant_id, invocation.workspace_id or "default"
        if not tenant:
            raise DeniedError("no_tenant", "工作区类工具只在租户作用域里可用")
        if not _SLUG.match(tenant) or not _SLUG.match(workspace):
            raise InvalidError("invalid_workspace", "租户或 workspace 的格式不对")
        return workspace

    def _workspace(self, invocation: ToolInvocation) -> Path:
        workspace = self._workspace_id(invocation)
        if self.root is None:
            raise InvalidError("unsupported_workspace", "这个 Worker 没有配工作区目录，租户的沙箱后端也不是存档模式")
        root = self.root / invocation.tenant_id / workspace
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve()

    async def _archived(self, invocation: ToolInvocation) -> bool:
        """租户的沙箱后端是存档模式（D-06）：工具在承载工作区的环境里执行。"""
        if self.config.shell_sandbox != "sandbox" or self.broker is None or not invocation.tenant_id:
            return False
        return await self.broker.archive_mode_for(invocation.tenant_id)

    @staticmethod
    def _env_path(path: Any, *, allow_root: bool = False) -> str:
        """存档模式下的路径：规则同 _resolve（经符号链接出界由环境里的 agent 管：它只在沙箱自己的文件系统里）。"""
        if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
            raise InvalidError("path_outside_workspace", "路径要相对工作区，用 / 分隔")
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or (pure.parts and ":" in pure.parts[0]):
            raise InvalidError("path_outside_workspace", f"路径 {path} 不在工作区里")
        rel = "/".join(p for p in pure.parts if p != ".")
        if not rel and not allow_root:
            raise InvalidError("path_outside_workspace", "要给工作区里的一个文件")
        return rel

    async def _in_env(self, invocation: ToolInvocation, *, writable: bool, run: Any) -> Any:
        """在承载工作区的环境里做一件事（存档模式）：按会话复用环境（会话级沙箱打开时），复用的环境在后端里没了就换一个再做一次。"""
        assert self.broker is not None
        workspace = self._workspace_id(invocation)
        tenant = TenantContext(tenant_id=invocation.tenant_id, workspace_id=workspace, session_id=invocation.session_id, user_id=invocation.user_id)
        spec = SandboxSpec(
            runtime=self.config.shell_runtime, cpu_millis=parse_cpu_millis(self.config.shell_cpus), memory_mb=parse_memory_mb(self.config.shell_memory),
            network="none", idle_timeout=timedelta(minutes=5), workspace_id=workspace, session_id=invocation.session_id,
        )
        reuse = self.config.session_sandboxes and bool(invocation.session_id) and self.broker.ledger is not None
        busy = timedelta(minutes=2)
        lease = await self.broker.acquire(tenant, spec, task_id=invocation.task_id, writable=writable, reuse=reuse, busy_for=busy)
        done = False
        try:
            try:
                out = await run(lease.box, lease.env_id)
            except NotFoundError as err:
                if not lease.reused or err.code != "unknown_env":
                    raise
                await self.broker.discard(lease, "后端里已经没有这个环境（被回收或节点重启）")
                lease = await self.broker.acquire(tenant, spec, task_id=invocation.task_id, writable=writable, reuse=reuse, busy_for=busy)
                out = await run(lease.box, lease.env_id)
            done = True
        finally:
            try:
                await self.broker.release(lease, keep=reuse)
            except ProviderError:
                # 写的工具的改动没存进快照：照实报错（读的工具还环境失败不影响结果）。
                if done and writable:
                    raise
        return out

    @staticmethod
    def _resolve(root: Path, path: Any) -> Path:
        if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
            raise InvalidError("path_outside_workspace", "路径要相对工作区，用 / 分隔")
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or (pure.parts and ":" in pure.parts[0]):
            raise InvalidError("path_outside_workspace", f"路径 {path} 不在工作区里")
        target = root.joinpath(*pure.parts).resolve() if pure.parts else root
        if target != root and not target.is_relative_to(root):
            raise InvalidError("path_outside_workspace", f"路径 {path} 不在工作区里")
        return target

    @staticmethod
    def _relative(root: Path, target: Path) -> str:
        return target.relative_to(root).as_posix() if target != root else "."

    # 工具

    async def read(self, invocation: ToolInvocation) -> Any:
        args = invocation.arguments
        if await self._archived(invocation):
            rel = self._env_path(args["path"])

            async def fetch(box: Any, env_id: str) -> bytes:
                try:
                    return await box.read_file(env_id, rel)
                except NotFoundError as err:
                    if err.code == "unknown_env":
                        raise
                    raise NotFoundError("file_not_found", f"工作区里没有文件 {args['path']}") from None

            return self._window(rel, await self._in_env(invocation, writable=False, run=fetch), args)
        root = self._workspace(invocation)
        target = self._resolve(root, args["path"])
        if not target.is_file():
            raise NotFoundError("file_not_found", f"工作区里没有文件 {args['path']}")
        return self._window(self._relative(root, target), target.read_bytes(), args)

    def _window(self, path: str, data: bytes, args: Any) -> dict[str, Any]:
        lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
        start = int(args.get("start_line") or 1)
        count = int(args.get("max_lines") or 400)
        window = lines[start - 1 : start - 1 + count]
        content = "".join(window)
        truncated = start - 1 + count < len(lines)
        if len(content) > self.config.output_limit:
            content, truncated = content[: self.config.output_limit], True
        return {
            "path": path,
            "content": content,
            "start_line": start,
            "end_line": start + len(window) - 1,
            "total_lines": len(lines),
            "truncated": truncated,
        }

    async def list(self, invocation: ToolInvocation) -> Any:
        if await self._archived(invocation):
            rel = self._env_path(invocation.arguments.get("path", "."), allow_root=True)

            async def listing(box: Any, env_id: str) -> Any:
                try:
                    return await box.list_dir(env_id, rel)
                except NotFoundError as err:
                    if err.code == "unknown_env":
                        raise
                    raise NotFoundError("dir_not_found", "工作区里没有这个目录") from None

            items = await self._in_env(invocation, writable=False, run=listing)
            entries = [{"name": e.name, "type": e.type, **({"size": e.size} if e.type == "file" else {})} for e in items]
            return {"path": rel or ".", "entries": entries}
        root = self._workspace(invocation)
        target = self._resolve(root, invocation.arguments.get("path", "."))
        if not target.is_dir():
            raise NotFoundError("dir_not_found", "工作区里没有这个目录")
        entries = []
        for child in sorted(target.iterdir(), key=lambda p: p.name)[:1000]:
            if child.is_dir():
                entries.append({"name": child.name, "type": "dir"})
            elif child.is_file():
                entries.append({"name": child.name, "type": "file", "size": child.stat().st_size})
        return {"path": self._relative(root, target), "entries": entries}

    async def grep(self, invocation: ToolInvocation) -> Any:
        args = invocation.arguments
        if await self._archived(invocation):
            rel = self._env_path(args.get("path", "."), allow_root=True)
            limit = int(args.get("max_matches") or 100)

            async def search(box: Any, env_id: str) -> Any:
                return await box.grep(env_id, str(args["pattern"]), path=rel, max_matches=limit)

            matches, truncated = await self._in_env(invocation, writable=False, run=search)
            return {"matches": [{"path": m.path, "line": m.line, "text": m.text} for m in matches], "truncated": truncated}
        root = self._workspace(invocation)
        try:
            pattern = re.compile(args["pattern"])
        except re.error as err:
            raise InvalidError("invalid_pattern", f"正则表达式不合法：{err}") from None
        target = self._resolve(root, args.get("path", "."))
        limit = int(args.get("max_matches") or 100)
        files = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts)
        matches: list[dict[str, Any]] = []
        for file in files:
            if not file.resolve().is_relative_to(root) or file.stat().st_size > 1024 * 1024:
                continue
            data = file.read_bytes()
            if b"\x00" in data[:1024]:
                continue  # 二进制文件
            for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
                if pattern.search(line):
                    matches.append({"path": self._relative(root, file), "line": number, "text": line[:500]})
                    if len(matches) >= limit:
                        return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    async def edit(self, invocation: ToolInvocation) -> Any:
        args = invocation.arguments
        if await self._archived(invocation):
            rel = self._env_path(args["path"])

            async def change(box: Any, env_id: str) -> Any:
                try:
                    old: bytes | None = await box.read_file(env_id, rel)
                except NotFoundError as err:
                    if err.code == "unknown_env":
                        raise
                    old = None
                data = self._edited(args, old)
                await box.write_file(env_id, rel, data)
                return {"path": rel, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "created": old is None}

            return await self._in_env(invocation, writable=True, run=change)
        root = self._workspace(invocation)
        target = self._resolve(root, args["path"])
        existed = target.is_file()
        data = self._edited(args, target.read_bytes() if existed else None)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {"path": self._relative(root, target), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "created": not existed}

    @staticmethod
    def _edited(args: Any, old: bytes | None) -> bytes:
        """repo_edit 的新内容：给了 old 就把它唯一的一处替换成 new（文件要存在），否则 new 是整个文件。"""
        new = str(args["new"])
        if "old" not in args:
            return new.encode("utf-8")
        if old is None:
            raise NotFoundError("file_not_found", f"工作区里没有文件 {args['path']}")
        text = old.decode("utf-8")
        count = text.count(args["old"])
        if count == 0:
            raise InvalidError("edit_no_match", "文件里找不到 old")
        if count > 1:
            raise InvalidError("edit_ambiguous", f"old 在文件里出现了 {count} 次，要能唯一定位")
        return text.replace(args["old"], new, 1).encode("utf-8")

    async def shell(self, invocation: ToolInvocation) -> Any:
        args = invocation.arguments
        argv = [str(a) for a in args["argv"]]
        timeout = min(int(args.get("timeout_s") or self.config.shell_timeout_s), self.config.max_shell_timeout_s)
        if await self._archived(invocation):
            # 存档模式（D-06）：不碰 Worker 的工作区目录；项目环境（D-17）要读工作区卷，这里不用。
            return await self._sandbox_shell(invocation, self._workspace_id(invocation), argv, timeout, None)
        root = self._workspace(invocation)
        if self.config.shell_sandbox == "docker":
            return await self._docker_shell(root, argv, timeout)
        if self.config.shell_sandbox == "sandbox":
            return await self._sandbox_shell(invocation, root.name, argv, timeout, root)
        env = {k: os.environ[k] for k in _SHELL_ENV if k in os.environ}
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=root, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except (FileNotFoundError, PermissionError, NotADirectoryError) as err:
            raise InvalidError("command_not_found", f"启动不了 {argv[0]}（{type(err).__name__}）") from None
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            stdout, stderr = await proc.communicate()
            return {"exit_code": None, "timed_out": True, **self._outputs(stdout, stderr)}
        return {"exit_code": proc.returncode, "timed_out": False, **self._outputs(stdout, stderr)}

    async def _docker_shell(self, root: Path, argv: list[str], timeout: int) -> Any:
        """在一次性容器里跑：只挂载本工作区，不联网，限资源；超时删除容器。启动不了 docker 按暂时性错误处理。"""
        c = self.config
        docker = shutil.which(c.shell_docker) or c.shell_docker
        name = "ar-shell-" + os.urandom(6).hex()
        cmd = [
            docker, "run", "--rm", "--name", name, "--network", "none",
            "--cpus", c.shell_cpus, "--memory", c.shell_memory, "--pids-limit", str(c.shell_pids),
            "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
            "-v", f"{root}:/workspace", "-w", "/workspace", "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
        ]
        if hasattr(os, "getuid"):  # Linux 上用 Worker 的用户跑，工作区里新建的文件不归 root
            cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
        cmd += [c.shell_image, *argv]
        # docker 的凭据助手与 docker 在同一目录；容器里的环境与 Worker 无关，这里只给 docker 客户端自己用。
        env = {k: os.environ[k] for k in (*_SHELL_ENV, "DOCKER_HOST", "DOCKER_CONFIG", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "ProgramData") if k in os.environ}
        env["PATH"] = str(Path(docker).parent) + os.pathsep + env.get("PATH", "")
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except (FileNotFoundError, PermissionError) as err:
            raise TransientError("sandbox_unavailable", f"启动不了 docker（{type(err).__name__}）") from None
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 30)
        except TimeoutError:
            remover = await asyncio.create_subprocess_exec(docker, "rm", "-f", name, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await remover.wait()
            proc.kill()
            stdout, stderr = await proc.communicate()
            return {"exit_code": None, "timed_out": True, "sandbox": "docker", **self._outputs(stdout, stderr)}
        # 125：docker 自己出错（镜像拉不到、daemon 不在）；不是命令的退出码。
        if proc.returncode == 125:
            raise TransientError("sandbox_unavailable", "Docker 沙箱启动失败：" + stderr.decode("utf-8", errors="replace")[-500:])
        return {"exit_code": proc.returncode, "timed_out": False, "sandbox": "docker", **self._outputs(stdout, stderr)}

    async def _sandbox_shell(self, invocation: ToolInvocation, workspace: str, argv: list[str], timeout: int, root: Path | None) -> Any:
        """经 Sandbox 接口（SandboxBroker）：按租户策略选后端（暂时不可用时按回退列表换），环境绑定本 workspace、断网。
        会话级沙箱（D-13）打开时按（工作区，会话）复用环境，用完不删；否则每条命令一个环境，跑完销毁。复用的环境在后端里
        没了（被回收）时，记为丢失、换一个新环境再跑一次，工作区的文件不受影响。

        计量（docs/plan/cloud-deploy.md D-07、D-10、D-11）：每次运行一个 runtime.sandbox span，记后端、隔离强度、环境号、
        是否复用与回退、执行时间与 CPU 时间（后端量得出时），一次性的环境另记存活时间；环境的存活时间与花费在环境表里。
        不记命令与输出。存档模式（root 为 None，D-06）时环境建好后从快照恢复、一次性的环境用完先存档，见 SandboxBroker。"""
        assert self.broker is not None
        tenant = TenantContext(tenant_id=invocation.tenant_id, workspace_id=workspace, session_id=invocation.session_id, user_id=invocation.user_id)
        c = self.config
        spec = SandboxSpec(
            runtime=c.shell_runtime,
            cpu_millis=parse_cpu_millis(c.shell_cpus),
            memory_mb=parse_memory_mb(c.shell_memory),
            network="none",
            idle_timeout=timedelta(seconds=timeout + 120),
            workspace_id=workspace,
            session_id=invocation.session_id,
        )
        reuse = c.session_sandboxes and bool(invocation.session_id) and self.broker.ledger is not None
        busy = timedelta(seconds=timeout + 60)
        environment = await self._environment(tenant, root) if root is not None else None
        if environment is not None and environment.image:
            spec = replace(spec, image=environment.image)
        attributes: dict[str, Any] = {"gen_ai.tool.name": "shell_run", "sandbox.cpu_millis": spec.cpu_millis, "sandbox.memory_mb": spec.memory_mb}
        if environment is not None:
            attributes["sandbox.environment"] = environment.state
        span_cm = self.telemetry.bind(tenant).start_span("runtime.sandbox", attributes=attributes) if self.telemetry else contextlib.nullcontext()
        archive_error: dict[str, str] | None = None
        async with span_cm as span:
            started = time.monotonic()
            try:
                lease = await self.broker.acquire(tenant, spec, task_id=invocation.task_id, writable=True, reuse=reuse, busy_for=busy)
            except InvalidError as err:
                if err.code != "unsupported_image" or environment is None:
                    raise
                # 项目环境的镜像落不到这个后端（例如回退到了托管后端）：退回 universal，并说明。
                spec = replace(spec, image="")
                environment = replace(environment, state="fallback", image="", note=f"{err.message}，这次用 universal")
                lease = await self.broker.acquire(tenant, spec, task_id=invocation.task_id, writable=True, reuse=reuse, busy_for=busy)
            ran = time.monotonic()
            # 新建了环境时借到它花的时间（冷启动）：进结果，tool_loop 的进度事件 tool_status 带给用户看；复用的环境没有这一项。
            startup_ms = None if lease.reused else int((ran - started) * 1000)
            result = None
            try:
                try:
                    result = await lease.box.exec(lease.env_id, argv, timeout=timedelta(seconds=timeout), env=lease.env or None)
                except NotFoundError:
                    if not lease.reused:
                        raise
                    await self.broker.discard(lease, "后端里已经没有这个环境（被回收或节点重启）")
                    retry_at = time.monotonic()
                    lease = await self.broker.acquire(tenant, spec, task_id=invocation.task_id, writable=True, reuse=reuse, busy_for=busy)
                    ran = time.monotonic()
                    startup_ms = None if lease.reused else int((ran - retry_at) * 1000)
                    result = await lease.box.exec(lease.env_id, argv, timeout=timedelta(seconds=timeout), env=lease.env or None)
            except InvalidError as err:
                if err.code == "command_not_found":
                    raise InvalidError("command_not_found", f"启动不了 {argv[0]}") from None
                raise
            finally:
                exec_ms = int((time.monotonic() - ran) * 1000)
                try:
                    await self.broker.release(lease, result=result, keep=reuse)
                except ProviderError as err:
                    if lease.archive and lease.writable:
                        # 存档模式下这次的改动没存进快照（D-06）：告诉调用方，不当作成功悄悄丢掉。
                        archive_error = {"code": err.code, "message": err.message}
                if span is not None:
                    meter: dict[str, Any] = {**lease.attributes, "sandbox.exec_ms": exec_ms}
                    if not reuse:
                        meter["sandbox.lifetime_ms"] = int((time.monotonic() - started) * 1000)
                    if lease.record is not None and lease.record.state == "destroyed":
                        meter["sandbox.cost_usd"] = lease.record.cost_usd
                    if result is not None:
                        meter["sandbox.timed_out"] = result.timed_out
                        if result.cpu_time is not None:
                            meter["sandbox.cpu_ms"] = int(result.cpu_time.total_seconds() * 1000)
                    span.set_attributes(meter)
        out = {"exit_code": result.exit_code, "timed_out": result.timed_out, "sandbox": lease.backend.name, **self._outputs(result.stdout, result.stderr)}
        if startup_ms is not None:
            out["sandbox_startup_ms"] = startup_ms
        if lease.record is not None and lease.record.state == "destroyed" and lease.record.cost_usd > 0:
            # 一次性环境的花费（按后端价目）：Node Executor 计入这个 Node 的 meta.cost_usd，进 Round 与 Task 的花费。
            out["sandbox_cost_usd"] = lease.record.cost_usd
        if environment is not None:
            # 仓库自带环境定义时说明用的是哪个（D-17）：没用上时写明原因，不悄悄退回。
            out["environment"] = environment.as_output()
        if archive_error is not None:
            out["workspace_error"] = archive_error
        return out

    async def _environment(self, tenant: TenantContext, root: Path) -> Resolution | None:
        """按项目定制的运行环境（D-17）：只对首选后端能用环境镜像的租户查（K8s，或登记里写了 custom_images: true）；
        仓库没有环境定义时 None。"""
        manager = self.broker.environments if self.broker is not None else None
        if manager is None:
            return None
        policy = await self.broker.sandboxes.policy_for(tenant.tenant_id)  # type: ignore[union-attr]
        backend = self.broker.sandboxes.spec(policy.backend)  # type: ignore[union-attr]
        if backend.get("kind") != "k8s" and backend.get("custom_images") is not True:
            return None
        resolution = await manager.resolve(tenant, workspace_reader(root))
        return None if resolution.state == "none" else resolution

    def _outputs(self, stdout: bytes, stderr: bytes) -> dict[str, Any]:
        limit = self.config.output_limit
        out, err = stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
        return {"stdout": out[-limit:], "stderr": err[-limit:], "truncated": len(out) > limit or len(err) > limit}


# --- web_fetch ---


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class _Fetcher:
    def __init__(self, config: PlatformToolsConfig) -> None:
        self.config = config
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def tool(self) -> tuple[ToolSpec, ToolHandler]:
        return (
            _spec("web_fetch", Effect.READ, "用 HTTP GET 取一个网页的文本（不跟随重定向）", {
                "url": {"type": "string", "minLength": 1, "description": "http 或 https 地址"},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": self.config.fetch_limit},
            }, ["url"], self.config.fetch_timeout_s + 10),
            self.fetch,
        )

    def _allowed(self, host: str) -> bool:
        for entry in self.config.fetch_hosts:
            entry = entry.strip().lower()
            if entry == "*" or entry == host or (entry.startswith("*.") and host.endswith(entry[1:])):
                return True
        return False

    async def _check_addresses(self, host: str, port: int) -> None:
        if self.config.allow_private_fetch:
            return
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
        except OSError:
            raise TransientError("dns_failed", f"解析不了主机 {host}") from None
        for info in infos:
            ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                raise DeniedError("private_address", f"{host} 解析到内网地址，不访问")

    async def fetch(self, invocation: ToolInvocation) -> Any:
        url = str(invocation.arguments["url"])
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise InvalidError("invalid_url", "只能取 http 或 https 地址")
        host = parts.hostname.lower()
        if not self._allowed(host):
            raise DeniedError("host_not_allowed", f"主机 {host} 不在 web_fetch 的名单里")
        await self._check_addresses(host, parts.port or (443 if parts.scheme == "https" else 80))
        limit = int(invocation.arguments.get("max_bytes") or self.config.fetch_limit)
        return await asyncio.to_thread(self._get, url, limit)

    def _get(self, url: str, limit: int) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"User-Agent": "agentruntime-web-fetch/0.1"})
        try:
            response = self.opener.open(request, timeout=self.config.fetch_timeout_s)
        except urllib.error.HTTPError as err:
            response = err  # 3xx（不跟随）、4xx、5xx：照样返回状态码与正文
        except (urllib.error.URLError, OSError) as err:
            raise TransientError("fetch_failed", f"取不到 {url}（{type(err).__name__}）") from None
        with response:
            status = int(response.status if hasattr(response, "status") else response.code)
            headers = response.headers
            body = response.read(limit + 1)
        content_type = headers.get("Content-Type", "") if headers else ""
        out: dict[str, Any] = {"url": url, "status": status, "content_type": content_type, "truncated": len(body) > limit}
        body = body[:limit]
        if not content_type or content_type.lower().startswith(_TEXT_TYPES):
            charset = headers.get_content_charset() if headers else None
            out["text"] = body.decode(charset or "utf-8", errors="replace")[:limit]
        else:
            out["text"] = ""
        location = headers.get("Location") if headers else None
        if location:
            out["location"] = location
        return out


# --- context_read ---


class _ContextReader:
    def __init__(self, objects: ObjectStoreProvider, config: PlatformToolsConfig) -> None:
        self.objects = objects
        self.config = config

    def tool(self) -> tuple[ToolSpec, ToolHandler]:
        limit = self.config.output_limit
        return (
            _spec(CONTEXT_READ, Effect.READ, "按路径取回上下文里被外置或遮蔽的内容（obj:// 路径），以及 Node 的外置结果；内容长时用 offset 分段取", {
                "path": {"type": "string", "minLength": 1, "description": "上下文里给出的 obj:// 路径"},
                "offset": {"type": "integer", "minimum": 0, "description": "从第几个字符开始，默认 0"},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": limit, "description": f"最多取多少个字符，默认 {self.config.context_read_chars}"},
            }, ["path"], 30),
            self.read,
        )

    async def read(self, invocation: ToolInvocation) -> Any:
        if not invocation.tenant_id:
            raise DeniedError("no_tenant", "context_read 只在租户作用域里可用")
        uri = str(invocation.arguments["path"])
        store = self.objects.bind(TenantContext(tenant_id=invocation.tenant_id, workspace_id=invocation.workspace_id))
        try:
            key = store.key_of(uri)
        except DeniedError:
            raise NotFoundError("not_found", "没有这个路径") from None  # 别的租户的路径：不透露它是否存在
        if not key.startswith(_CONTEXT_PREFIXES):
            raise DeniedError("path_not_allowed", "context_read 只能取回上下文里外置的内容与 Node 的外置结果")
        text = (await store.get(key)).data.decode("utf-8", errors="replace")
        offset = int(invocation.arguments.get("offset") or 0)
        part = text[offset : offset + int(invocation.arguments.get("max_chars") or self.config.context_read_chars)]
        return {"path": uri, "content": part, "offset": offset, "total_chars": len(text), "truncated": offset + len(part) < len(text)}
