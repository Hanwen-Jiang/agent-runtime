"""E2B 沙箱里 envd 的客户端（e2b 分支，docs/plan/e2b-backend.md 第 3 节）。只用标准库。

envd 跑在每个 E2B 沙箱（Firecracker microVM）里，监听 49983：
- 进程：Connect 协议（connect-rpc，JSON 编码）的 process.Process 服务。Start 是服务端流（请求与每个响应都是
  “1 字节标志 + 4 字节长度 + JSON”的信封，标志带 0x02 的是流的结尾，里面可能有 error）；SendInput、CloseStdin、
  SendSignal 是一元调用（普通的 POST application/json）。
- 文件：HTTP 的 /files（GET 取文件；POST application/octet-stream 写文件，父目录不存在时 envd 会建）。
- 认证：envd 的令牌放 X-Access-Token（E2B 的 api 在建沙箱、connect 时给出）；进程以哪个用户跑由
  Authorization: Basic base64("<用户>:") 决定，文件的 username 参数同理。

经 E2B 的 client-proxy 访问：按头路由（E2b-Sandbox-Id、E2b-Sandbox-Port，自托管的 Embed 就这样）或按主机名
（https://49983-<沙箱 id>.<域名>）。怎么连由 EnvdEndpoint 给出。

协议按 E2B 运行时的提交 67b3182 核对（packages/envd/spec/process/process.proto、envd.yaml）。

错误：HTTP 400 与 Connect 的 invalid_argument、failed_precondition、out_of_range 是 invalid；404 与 not_found 是
not_found；401、403 与 unauthenticated、permission_denied 是 denied；其余（连不上、5xx、unavailable 等）是 transient。
启动不了的命令（找不到可执行文件）是 invalid（command_not_found）。令牌不进错误信息。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError, ProviderError, TransientError
from agentruntime_core.providers.sandbox import ExecResult

ENVD_PORT = 49983
_END_STREAM = 0x02
# 每路输出最多留这么多字节（保留末尾；平台工具再按自己的上限截断）。
OUTPUT_CAP = 8 * 1024 * 1024
_INVALID_CODES = {"invalid_argument", "failed_precondition", "out_of_range"}
_DENIED_CODES = {"unauthenticated", "permission_denied"}
_NOT_FOUND_HINTS = ("executable file not found", "no such file or directory")


@dataclass(frozen=True)
class EnvdEndpoint:
    # client-proxy 的根地址（按头路由时）或 https://49983-<id>.<域名>（按主机名时），不以 / 结尾。
    base_url: str
    access_token: str
    # 路由用的头（按头路由时是 E2b-Sandbox-Id、E2b-Sandbox-Port），不含令牌。
    headers: Mapping[str, str] = field(default_factory=dict)
    user: str = "user"


def header_routed(sandbox_url: str, sandbox_id: str, access_token: str, *, user: str = "user") -> EnvdEndpoint:
    return EnvdEndpoint(
        sandbox_url.rstrip("/"), access_token, {"E2b-Sandbox-Id": sandbox_id, "E2b-Sandbox-Port": str(ENVD_PORT)}, user
    )


def host_routed(domain: str, sandbox_id: str, access_token: str, *, user: str = "user") -> EnvdEndpoint:
    return EnvdEndpoint(f"https://{ENVD_PORT}-{sandbox_id}.{domain}", access_token, {}, user)


def envelope(message: Mapping[str, Any], flags: int = 0) -> bytes:
    data = json.dumps(message, separators=(",", ":")).encode()
    return struct.pack(">BI", flags, len(data)) + data


@dataclass
class _Run:
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    pid: int | None = None
    end: dict[str, Any] | None = None

    def add(self, buffer: bytearray, chunk: bytes) -> None:
        buffer += chunk
        if len(buffer) > OUTPUT_CAP:
            del buffer[: len(buffer) - OUTPUT_CAP]


class EnvdClient:
    def __init__(self, *, request_timeout: timedelta = timedelta(seconds=60), start_timeout: timedelta = timedelta(seconds=30)) -> None:
        self._request_timeout = request_timeout.total_seconds()
        self._start_timeout = start_timeout.total_seconds()

    # --- 文件 ---

    async def write_file(self, endpoint: EnvdEndpoint, path: str, data: bytes) -> None:
        query = urllib.parse.urlencode({"path": path, "username": endpoint.user})
        status, body = await asyncio.to_thread(
            self._http, endpoint, "POST", "/files?" + query, data, {"Content-Type": "application/octet-stream"}, self._request_timeout
        )
        _raise_for(status, body)

    async def read_file(self, endpoint: EnvdEndpoint, path: str) -> bytes:
        query = urllib.parse.urlencode({"path": path, "username": endpoint.user})
        status, body = await asyncio.to_thread(self._http, endpoint, "GET", "/files?" + query, None, {}, self._request_timeout)
        _raise_for(status, body)
        return body

    # --- 进程 ---

    async def run(
        self,
        endpoint: EnvdEndpoint,
        argv: Sequence[str],
        *,
        cwd: str,
        envs: Mapping[str, str],
        timeout: timedelta,
        stdin: bytes = b"",
    ) -> ExecResult:
        """跑一条命令（不经 shell），等它结束或超时。超时时发 SIGKILL，返回 timed_out，不抛异常。"""
        if not argv:
            raise InvalidError("invalid_argument", "argv 不能为空")
        loop = asyncio.get_running_loop()
        started: asyncio.Future[int] = loop.create_future()
        run = _Run()
        request = {
            "process": {"cmd": argv[0], "args": list(argv[1:]), "envs": dict(envs), "cwd": cwd},
            "stdin": bool(stdin),
        }

        def on_pid(pid: int) -> None:
            loop.call_soon_threadsafe(lambda: started.done() or started.set_result(pid))

        limit = timeout.total_seconds()
        reader = asyncio.ensure_future(asyncio.to_thread(self._start, endpoint, request, run, on_pid, limit + self._request_timeout))
        began = time.monotonic()
        done, _ = await asyncio.wait({started, reader}, timeout=self._start_timeout, return_when=asyncio.FIRST_COMPLETED)
        if not started.done():
            if reader.done():
                reader.result()  # 抛出启动时的错误
                raise TransientError("sandbox_bad_response", "envd 没有报告进程启动")
            raise TransientError("sandbox_unavailable", "envd 在时限内没有启动命令")
        pid = started.result()
        if stdin:
            try:
                await self._unary(endpoint, "SendInput", {"process": {"pid": pid}, "input": {"stdin": base64.b64encode(stdin).decode()}})
                await self._unary(endpoint, "CloseStdin", {"process": {"pid": pid}})
            except ProviderError:
                with contextlib.suppress(ProviderError):
                    await self._kill(endpoint, pid)
                raise
        remaining = max(0.0, limit - (time.monotonic() - began))
        done, _ = await asyncio.wait({reader}, timeout=remaining)
        if reader not in done:
            with contextlib.suppress(ProviderError):
                await self._kill(endpoint, pid)
            # 杀掉之后流会以 end 事件结束；等一会儿把已有的输出收齐。
            await asyncio.wait({reader}, timeout=10)
            if reader.done():
                with contextlib.suppress(BaseException):
                    reader.result()
            return ExecResult(exit_code=None, stdout=bytes(run.stdout), stderr=bytes(run.stderr), timed_out=True)
        end = reader.result()
        if end.get("error"):
            error = str(end["error"])
            if any(h in error.lower() for h in _NOT_FOUND_HINTS):
                raise InvalidError("command_not_found", f"启动不了 {argv[0]}")
        exited = bool(end.get("exited"))
        code = int(end.get("exitCode") or 0) if exited else -1
        return ExecResult(exit_code=code, stdout=bytes(run.stdout), stderr=bytes(run.stderr), timed_out=False)

    async def _kill(self, endpoint: EnvdEndpoint, pid: int) -> None:
        await self._unary(endpoint, "SendSignal", {"process": {"pid": pid}, "signal": "SIGNAL_SIGKILL"})

    async def _unary(self, endpoint: EnvdEndpoint, method: str, message: Mapping[str, Any]) -> dict[str, Any]:
        body = json.dumps(message).encode()
        headers = {"Content-Type": "application/json", "Connect-Protocol-Version": "1", **_user_header(endpoint)}
        status, data = await asyncio.to_thread(
            self._http, endpoint, "POST", f"/process.Process/{method}", body, headers, self._request_timeout
        )
        _raise_for(status, data)
        try:
            out = json.loads(data or b"{}")
        except ValueError:
            raise TransientError("sandbox_bad_response", "envd 返回的结果格式不对") from None
        return out if isinstance(out, dict) else {}

    def _start(
        self, endpoint: EnvdEndpoint, message: Mapping[str, Any], run: _Run, on_pid: Callable[[int], None], read_timeout: float
    ) -> dict[str, Any]:
        """在线程里读 Start 的响应流，直到 end 事件与流的结尾。返回 end 事件。"""
        headers = {
            "Content-Type": "application/connect+json",
            "Connect-Protocol-Version": "1",
            # 让 envd 定期发 keepalive，空闲很久的命令也不会被中间的代理断开。
            "Keepalive-Ping-Interval": "50",
            **_user_header(endpoint),
        }
        request = _request(endpoint, "POST", "/process.Process/Start", envelope(message), headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            response = opener.open(request, timeout=read_timeout)
        except urllib.error.HTTPError as err:
            _raise_for(err.code, err.read())
            raise TransientError("sandbox_unavailable", f"envd 拒绝了这个命令（HTTP {err.code}）") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
            raise TransientError("sandbox_unavailable", f"连不上沙箱（{type(err).__name__}）") from None
        end: dict[str, Any] | None = None
        with response:
            try:
                while True:
                    head = _read_exact(response, 5)
                    if head is None:
                        break
                    flags, length = struct.unpack(">BI", head)
                    payload = _read_exact(response, length) if length else b""
                    if payload is None:
                        raise TransientError("sandbox_bad_response", "envd 的响应流中途断了")
                    try:
                        message_out = json.loads(payload or b"{}")
                    except ValueError:
                        raise TransientError("sandbox_bad_response", "envd 返回的结果格式不对") from None
                    if flags & _END_STREAM:
                        error = (message_out or {}).get("error")
                        if error:
                            _raise_connect(error)
                        break
                    event = (message_out.get("event") or {}) if isinstance(message_out, dict) else {}
                    if "start" in event:
                        run.pid = int((event["start"] or {}).get("pid") or 0)
                        on_pid(run.pid)
                    elif "data" in event:
                        data = event["data"] or {}
                        if data.get("stdout"):
                            run.add(run.stdout, base64.b64decode(data["stdout"]))
                        if data.get("stderr"):
                            run.add(run.stderr, base64.b64decode(data["stderr"]))
                    elif "end" in event:
                        end = dict(event["end"] or {})
            except (TimeoutError, ConnectionError, OSError) as err:
                raise TransientError("sandbox_unavailable", f"读 envd 的输出时断了（{type(err).__name__}）") from None
        if end is None:
            raise TransientError("sandbox_bad_response", "envd 的响应流没有 end 事件")
        run.end = end
        return end

    @staticmethod
    def _http(
        endpoint: EnvdEndpoint, method: str, path: str, body: bytes | None, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        request = _request(endpoint, method, path, body, headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
            raise TransientError("sandbox_unavailable", f"连不上沙箱（{type(err).__name__}）") from None


def _request(endpoint: EnvdEndpoint, method: str, path: str, body: bytes | None, headers: Mapping[str, str]) -> urllib.request.Request:
    request = urllib.request.Request(endpoint.base_url + path, data=body, method=method)
    for k, v in {**endpoint.headers, **headers, "X-Access-Token": endpoint.access_token}.items():
        request.add_header(k, v)
    return request


def _user_header(endpoint: EnvdEndpoint) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{endpoint.user}:".encode()).decode()}


def _read_exact(response: Any, n: int) -> bytes | None:
    """读满 n 个字节；一开头就到了结尾时返回 None。"""
    chunks = bytearray()
    while len(chunks) < n:
        part = response.read(n - len(chunks))
        if not part:
            if not chunks:
                return None
            raise TransientError("sandbox_bad_response", "envd 的响应流中途断了")
        chunks += part
    return bytes(chunks)


def _raise_connect(error: Mapping[str, Any]) -> None:
    code = str(error.get("code") or "unknown")
    message = str(error.get("message") or "")
    if any(h in message.lower() for h in _NOT_FOUND_HINTS):
        raise InvalidError("command_not_found", "启动不了这个命令（找不到可执行文件）")
    if code in _INVALID_CODES:
        raise InvalidError("sandbox_rejected", message[:300] or "envd 拒绝了这个请求")
    if code == "not_found":
        raise NotFoundError("not_found", message[:300] or "不存在")
    if code in _DENIED_CODES:
        raise DeniedError("sandbox_denied", "没有权限访问这个沙箱")
    raise TransientError("sandbox_unavailable", f"沙箱暂时不可用（{code}）")


def _raise_for(status: int, body: bytes) -> None:
    if 200 <= status < 300:
        return
    message = ""
    connect_code = ""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            message = str(data.get("message") or "")
            connect_code = str(data.get("code") or "") if isinstance(data.get("code"), str) else ""
    except ValueError:
        pass
    if connect_code:
        _raise_connect({"code": connect_code, "message": message})
    if status in (400, 413, 422):
        raise InvalidError("sandbox_rejected", message[:300] or "沙箱拒绝了这个请求")
    if status == 404:
        raise NotFoundError("not_found", message[:300] or "不存在")
    if status in (401, 403):
        raise DeniedError("sandbox_denied", "没有权限访问这个沙箱")
    raise TransientError("sandbox_unavailable", f"沙箱暂时不可用（HTTP {status}）")


__all__ = ["ENVD_PORT", "EnvdClient", "EnvdEndpoint", "envelope", "header_routed", "host_routed"]
