"""MCP Gateway（设计书 4.9、附录 L.10；T-10，细化见 docs/plan/tool-driver.md 第 2 节第 5 条）。

按登记文件连 MCP Server（stdio，JSON-RPC 2.0：initialize → tools/list → tools/call；客户端自己实现，不引入新依赖），
把**登记了**的工具包装成内核的工具契约，登记进 ToolDriver（LocalToolDriverProvider）。内核只认内核契约：
参数校验、写操作要幂等键、幂等记录、超时、输出校验都走 ToolDriver 的同一套规则，处理函数只是把调用转给 MCP 会话。

- 登记文件：{"servers": [{"name", "command": [...], "env": {...}, "tools": [{"tool", "name", "version", "effect",
  "timeout_s", "tenants"}]}]}。effect 必填（MCP 没有这个字段，要人工补齐）；name 默认同 tool，version 默认 v1，
  timeout_s 默认 30；tenants 给了就只登记给这些租户。第一期不支持 permissions（凭据怎么交给 MCP Server 另行设计），
  写了就是登记文件不合格。说明与 input_schema 取自 Server；Server 声明了 outputSchema 就校验输出。
- 没登记的 MCP 工具不登记、不能调用；登记了但 Server 没有的跳过（skipped）；Server 起不来时它的工具都跳过，别的照常。
- 有结构化输出（structuredContent）就用它，否则把文本包成 {"text"}；isError 是 invalid（code tool_error）。
- 会话由网关集中管理，不进内核：会话断了（Server 退出）或一次调用被取消（超时）这次失败，下次调用时重启会话。
  Server 子进程的环境只带 PATH 等必需的变量与登记的 env，Worker 的其他环境变量不带进去。
- 第一期在 Worker 进程内运行（MCP Gateway 作为单独部署的边缘服务、HTTP 传输登记到后续）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.enums import Effect
from agentruntime_core.errors import InvalidError, TransientError
from agentruntime_core.providers.tool_driver import ToolHandler, ToolInvocation, ToolSpec

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
_NAME = re.compile(r"^[a-z][a-z0-9_.-]*$")
_VERSION = re.compile(r"^v[0-9]+$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_SERVER_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "LANG", "LC_ALL")
_TOOL_KEYS = {"tool", "name", "version", "effect", "timeout_s", "tenants", "permissions"}
_SERVER_KEYS = {"name", "command", "env", "tools", "startup_timeout_s"}
# 一行 JSON-RPC 消息的长度上限（工具结果可能很大）。
_LINE_LIMIT = 16 * 1024 * 1024


class McpRegistrationError(InvalidError, ValueError):
    """MCP 登记文件不合格。同时是 ValueError：Worker 启动时按配置错误报出。"""


@dataclass(frozen=True)
class McpTool:
    tool: str
    name: str
    version: str
    effect: Effect
    timeout: timedelta
    tenants: tuple[str, ...] = ()


@dataclass(frozen=True)
class McpServer:
    name: str
    command: tuple[str, ...]
    env: Mapping[str, str]
    tools: tuple[McpTool, ...]
    startup_timeout_s: float = 15.0


def _bad(message: str) -> McpRegistrationError:
    return McpRegistrationError("invalid_mcp_registration", message)


def _tool(server: str, raw: Any) -> McpTool:
    if not isinstance(raw, Mapping):
        raise _bad(f"{server} 的工具登记要是对象")
    unknown = set(raw) - _TOOL_KEYS
    if unknown:
        raise _bad(f"{server} 的工具登记有不认识的字段 {sorted(unknown)}")
    tool = raw.get("tool")
    if not isinstance(tool, str) or not tool:
        raise _bad(f"{server} 的工具登记缺 tool（Server 上的工具名）")
    name, version = raw.get("name", tool), raw.get("version", "v1")
    if not isinstance(name, str) or not _NAME.match(name):
        raise _bad(f"{server}/{tool} 的 name {name!r} 不合格（小写字母开头）")
    if not isinstance(version, str) or not _VERSION.match(version):
        raise _bad(f"{server}/{tool} 的 version {version!r} 要写成 vN")
    if "effect" not in raw:
        raise _bad(f"{server}/{tool} 缺 effect：MCP 没有这个字段，必须人工登记")
    try:
        effect = Effect(raw["effect"])
    except ValueError:
        raise _bad(f"{server}/{tool} 的 effect {raw['effect']!r} 不是附录 A 的取值") from None
    if raw.get("permissions"):
        raise _bad(f"{server}/{tool} 声明了 permissions：第一期 MCP 工具不支持权限")
    timeout = raw.get("timeout_s", 30)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise _bad(f"{server}/{tool} 的 timeout_s 要大于 0")
    tenants = raw.get("tenants") or []
    if not isinstance(tenants, list) or not all(isinstance(t, str) and _SLUG.match(t) for t in tenants):
        raise _bad(f"{server}/{tool} 的 tenants 要是租户 ID 的列表")
    return McpTool(tool=tool, name=name, version=version, effect=effect, timeout=timedelta(seconds=float(timeout)), tenants=tuple(tenants))


def load_registration(source: Mapping[str, Any] | Path) -> list[McpServer]:
    """读取并校验登记文件（或已经解析的 dict）。不合格时抛 McpRegistrationError。"""
    if isinstance(source, Path):
        try:
            data = json.loads(source.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise _bad(f"读不了 MCP 登记文件 {source}（{type(err).__name__}）") from None
    else:
        data = source
    servers = data.get("servers") if isinstance(data, Mapping) else None
    if not isinstance(servers, list):
        raise _bad("登记文件要有 servers 列表")
    out: list[McpServer] = []
    seen: set[tuple[str, str]] = set()
    for raw in servers:
        if not isinstance(raw, Mapping):
            raise _bad("servers 里的每一项要是对象")
        unknown = set(raw) - _SERVER_KEYS
        if unknown:
            raise _bad(f"Server 登记有不认识的字段 {sorted(unknown)}")
        name, command, env = raw.get("name"), raw.get("command"), raw.get("env") or {}
        if not isinstance(name, str) or not name:
            raise _bad("Server 要有 name")
        if not isinstance(command, list) or not command or not all(isinstance(c, str) and c for c in command):
            raise _bad(f"Server {name} 的 command 要是非空的字符串列表")
        if not isinstance(env, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise _bad(f"Server {name} 的 env 要是字符串到字符串")
        tools = tuple(_tool(name, t) for t in raw.get("tools") or [])
        for t in tools:
            if (t.name, t.version) in seen:
                raise _bad(f"工具 {t.name}@{t.version} 登记了两次")
            seen.add((t.name, t.version))
        out.append(McpServer(name=name, command=tuple(command), env=dict(env), tools=tools, startup_timeout_s=float(raw.get("startup_timeout_s", 15))))
    return out


class _McpError(Exception):
    """Server 返回的 JSON-RPC 错误。"""


class McpSession:
    """一个 MCP Server 的 stdio 会话：一次只有一个请求在途；断了或被取消就丢弃，下次调用时重启。"""

    def __init__(self, server: McpServer) -> None:
        self.server = server
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0
        self._broken = False

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        env = {k: os.environ[k] for k in _SERVER_ENV if k in os.environ}
        env.update(self.server.env)
        self._process = await asyncio.create_subprocess_exec(
            *self.server.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            limit=_LINE_LIMIT,
        )
        self._broken = False
        params = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "agentruntime-mcp-gateway", "version": "0.1"}}
        await asyncio.wait_for(self._exchange("initialize", params), timeout=self.server.startup_timeout_s)
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def _ensure(self) -> None:
        if self._broken or not self.alive:
            await self.close()
            await self.start()

    async def _send(self, message: Mapping[str, Any]) -> None:
        assert self._process is not None and self._process.stdin is not None
        self._process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await self._process.stdin.drain()

    async def _exchange(self, method: str, params: Mapping[str, Any]) -> Any:
        """发一个请求、等到同 id 的回应；Server 退出时抛 TransientError。被取消（超时）时会话作废。"""
        assert self._process is not None and self._process.stdout is not None
        self._next_id += 1
        request_id = self._next_id
        done = False
        try:
            await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    self._broken = True
                    raise TransientError("mcp_session_lost", f"MCP Server {self.server.name} 断开了")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, Mapping) or message.get("id") != request_id:
                    continue  # 通知或过期的回应
                done = True
                if "error" in message:
                    error = message["error"] or {}
                    raise _McpError(str(error.get("message") or "MCP 错误"))
                return message.get("result") or {}
        except (BrokenPipeError, ConnectionResetError):
            self._broken = True
            raise TransientError("mcp_session_lost", f"MCP Server {self.server.name} 断开了") from None
        finally:
            if not done and not self._broken:
                # 没等到回应就离开（多半是超时被取消）：之后的回应对不上号，会话作废，下次重启。
                self._broken = True
                if self.alive:
                    assert self._process is not None
                    self._process.kill()

    async def request(self, method: str, params: Mapping[str, Any]) -> Any:
        async with self._lock:
            await self._ensure()
            return await self._exchange(method, params)

    async def list_tools(self) -> list[Mapping[str, Any]]:
        tools: list[Mapping[str, Any]] = []
        cursor: str | None = None
        for _ in range(100):
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += [t for t in result.get("tools") or [] if isinstance(t, Mapping)]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call(self, tool: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            return await self.request("tools/call", {"name": tool, "arguments": dict(arguments)})
        except _McpError as err:
            raise InvalidError("tool_error", f"MCP 工具 {tool} 报错：{str(err)[:500]}") from None

    async def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        with contextlib.suppress(Exception):
            await process.wait()


def _handler(session: McpSession, tool: str) -> ToolHandler:
    async def handle(invocation: ToolInvocation) -> Any:
        result = await session.call(tool, invocation.arguments)
        texts = [str(c.get("text", "")) for c in result.get("content") or [] if isinstance(c, Mapping) and c.get("type") == "text"]
        if result.get("isError"):
            raise InvalidError("tool_error", f"MCP 工具 {tool} 报错：{' '.join(texts)[:500]}")
        if "structuredContent" in result:
            return result["structuredContent"]
        return {"text": "\n".join(texts)}

    return handle


class McpGateway:
    """进入时启动各 Server 的会话、列出工具，把登记了的包装后登记进 provider；退出时关闭会话。

    registered 是登记了的内核工具名；skipped 是登记文件里有、但没登记进去的（Server 没有这个工具或 Server 起不来）。
    """

    def __init__(self, provider: LocalToolDriverProvider, registration: Mapping[str, Any] | Path) -> None:
        self.provider = provider
        self.servers = load_registration(registration)
        self.registered: list[str] = []
        self.skipped: list[str] = []
        self.sessions: list[McpSession] = []

    async def __aenter__(self) -> McpGateway:
        for server in self.servers:
            session = McpSession(server)
            self.sessions.append(session)
            try:
                await session.start()
                offered = {str(t.get("name")): t for t in await session.list_tools()}
            except Exception as err:  # noqa: BLE001 — 一个 Server 起不来不影响别的
                logger.warning("MCP Server %s 起不来（%s），它的工具都不登记", server.name, type(err).__name__)
                await session.close()
                self.skipped += [t.name for t in server.tools]
                continue
            for tool in server.tools:
                info = offered.get(tool.tool)
                if info is None:
                    logger.warning("MCP Server %s 没有工具 %s，跳过 %s", server.name, tool.tool, tool.name)
                    self.skipped.append(tool.name)
                    continue
                spec = ToolSpec(
                    name=tool.name,
                    version=tool.version,
                    description=str(info.get("description") or tool.tool),
                    input_schema=dict(info.get("inputSchema") or {"type": "object"}),
                    output_schema=dict(info.get("outputSchema") or {}),
                    effect=tool.effect,
                    permissions=frozenset(),
                    timeout=tool.timeout,
                )
                handler = _handler(session, tool.tool)
                for tenant in tool.tenants or (None,):
                    self.provider.register(spec, handler, tenant_id=tenant)
                self.registered.append(tool.name)
        return self

    async def __aexit__(self, *exc: object) -> None:
        for session in self.sessions:
            await session.close()
