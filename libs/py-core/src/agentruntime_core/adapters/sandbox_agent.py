"""sandbox-agent 的客户端（agent 在 services/go-sandbox-agent）。K8s 的沙箱 Pod 与 Cloudflare 的 Container 里跑的是同一个 agent，
Sandbox 的这两个适配器都经它执行命令、读写文件（计划 docs/plan/cloud-deploy.md 的 D-05、D-07）。只用标准库。

怎么连到 agent 由适配器给出（AgentEndpoint）：集群内直连 Pod 的地址；集群外经 K8s API Server 的 pods/proxy；
Cloudflare 经桥（Worker）转发。agent 的令牌放 X-Sandbox-Token 头，额外的头（API Server、桥的认证）另放。

错误：agent 的 400、409（导入存档时工作目录不空）、413 是 invalid；404 是 not_found；401、403 是 denied（令牌或权限配错了）；
连不上、传输中断、5xx、429 是 transient。存档的导出与导入（D-06）走流式的请求，不把整个工作区读进内存。

取消：请求在线程里阻塞，协程取消了线程还在等，所以取消时直接断开连接（_Connection），agent 看到断开就杀掉命令的进程组。中间隔着
K8s API Server 或 Cloudflare 的桥时断开不一定传得到，所以 exec 另发 POST /v1/exec/cancel（docs/plan/running-task-interaction.md 第 2 节）。
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import json
import re
import secrets
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import IO, Any, TypeVar

from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError, ProviderError, TransientError
from agentruntime_core.providers.sandbox import DirEntry, ExecNotStopped, ExecResult, FileEntry, GrepMatch, check_exec_env

TOKEN_HEADER = "X-Sandbox-Token"
CANCEL_TIMEOUT_S = 3.0
_DRIVE = re.compile(r"^[A-Za-z]:")
T = TypeVar("T")


def _positive(value: int) -> int:
    if not isinstance(value, int) or value <= 0:
        raise InvalidError("invalid_argument", "max_bytes 要是正整数")
    return value


@dataclass(frozen=True)
class AgentEndpoint:
    # agent 的根地址，不以 / 结尾；后面接 /v1/exec、/v1/files/…、/healthz。
    base_url: str
    token: str
    headers: Mapping[str, str] = field(default_factory=dict)
    ssl_context: ssl.SSLContext | None = None


def check_path(path: str) -> str:
    """与 agent、开发实现相同的路径规则；不合法时在发请求之前就报 invalid。"""
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path or path.startswith("/") or _DRIVE.match(path):
        raise InvalidError("invalid_path", "路径必须是工作目录内的相对路径")
    if any(p in ("", ".", "..") for p in path.split("/")):
        raise InvalidError("invalid_path", "路径不得含空段、. 或 ..")
    return path


class _Connection:
    """一次请求的连接：取消时从别的线程断开，阻塞在读响应的线程随之出错返回。还在建连接时取消，连上之后马上断开。"""

    def __init__(self) -> None:
        self._conn: http.client.HTTPConnection | None = None
        self._aborted = False

    def _made(self, conn: http.client.HTTPConnection) -> None:
        self._conn = conn
        if self._aborted:
            self.abort()
            raise ConnectionAbortedError("请求被取消")

    def abort(self) -> None:
        self._aborted = True
        sock = self._conn.sock if self._conn is not None else None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def opener(self, context: ssl.SSLContext | None) -> urllib.request.OpenerDirector:
        watch = self

        class Plain(http.client.HTTPConnection):
            def connect(self) -> None:
                super().connect()
                watch._made(self)

        class Secure(http.client.HTTPSConnection):
            def connect(self) -> None:
                super().connect()
                watch._made(self)

        class PlainHandler(urllib.request.HTTPHandler):
            def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
                return self.do_open(Plain, req)

        class SecureHandler(urllib.request.HTTPSHandler):
            def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
                return self.do_open(Secure, req, context=context)

        return urllib.request.build_opener(urllib.request.ProxyHandler({}), PlainHandler(), SecureHandler())


class AgentClient:
    def __init__(self, *, request_timeout: timedelta = timedelta(seconds=60)) -> None:
        self._request_timeout = request_timeout

    async def exec(
        self, endpoint: AgentEndpoint, argv: Sequence[str], *, timeout: timedelta, stdin: bytes = b"", env: Mapping[str, str] | None = None
    ) -> ExecResult:
        if not argv:
            raise InvalidError("invalid_argument", "argv 不能为空")
        ms = max(1, int(timeout.total_seconds() * 1000))
        exec_id = secrets.token_hex(16)
        request: dict[str, object] = {"argv": list(argv), "stdin": _b64(stdin), "timeout_ms": ms, "exec_id": exec_id}
        extra = check_exec_env(env)
        if extra:
            request["env"] = extra
        body = json.dumps(request).encode()
        try:
            status, data = await self._call(
                endpoint, "POST", "/v1/exec", body, {"Content-Type": "application/json"}, timeout.total_seconds() + self._request_timeout.total_seconds()
            )
        except asyncio.CancelledError:
            if not await self._cancel_exec(endpoint, exec_id):
                raise ExecNotStopped() from None
            raise
        self.raise_for(status, data)
        try:
            out = json.loads(data)
            cpu_ms = out.get("cpu_ms")
            return ExecResult(
                exit_code=out["exit_code"], stdout=_unb64(out.get("stdout")), stderr=_unb64(out.get("stderr")), timed_out=bool(out["timed_out"]),
                cpu_time=timedelta(milliseconds=int(cpu_ms)) if isinstance(cpu_ms, int) else None,
            )
        except (ValueError, KeyError, TypeError):
            raise TransientError("sandbox_bad_response", "沙箱返回的结果格式不对") from None

    async def _cancel_exec(self, endpoint: AgentEndpoint, exec_id: str) -> bool:
        """让 agent 停掉这条命令；等一小会儿，调用方接下来可能要还原工作区。返回 agent 确认了没有：旧的 agent 没有这个接口，只剩断开连接这一条，经桥时不一定传得到。"""
        body = json.dumps({"exec_id": exec_id}).encode()
        try:
            status, _ = await asyncio.wait_for(self._call(endpoint, "POST", "/v1/exec/cancel", body, {"Content-Type": "application/json"}, CANCEL_TIMEOUT_S), CANCEL_TIMEOUT_S)
        except (TransientError, TimeoutError):
            return False
        return 200 <= status < 300

    async def write_file(self, endpoint: AgentEndpoint, path: str, data: bytes) -> None:
        status, body = await self._call(endpoint, "PUT", "/v1/files/" + _quote(check_path(path)), data, {"Content-Type": "application/octet-stream"})
        self.raise_for(status, body)

    async def read_file(self, endpoint: AgentEndpoint, path: str) -> bytes:
        status, body = await self._call(endpoint, "GET", "/v1/files/" + _quote(check_path(path)), None, {})
        self.raise_for(status, body)
        return body

    # --- 工作区的存档模式与在环境里执行的工作区类工具（D-06；agent 一侧见 internal/agent/archive.go）---

    async def export_archive(self, endpoint: AgentEndpoint, out: IO[bytes], *, max_bytes: int) -> int:
        """GET /v1/archive：边读边写进 out（不整个读进内存），返回字节数。tar 比文件总大小多出头部与对齐，上限放宽一些。
        agent 中途出错时断开连接，这里读到不完整的响应：transient。"""
        limit = int(max_bytes) + int(max_bytes) // 8 + (1 << 20)

        def receive(resp: Any) -> int:
            copied = 0
            while chunk := resp.read(1 << 20):
                copied += len(chunk)
                if copied > limit:
                    raise InvalidError("too_large", "导出的工作区超过上限")
                out.write(chunk)
            return copied

        return await self._stream(endpoint, "GET", f"/v1/archive?max_bytes={_positive(max_bytes)}", None, 0, {}, receive)

    async def import_archive(self, endpoint: AgentEndpoint, src: IO[bytes], *, size: int, max_bytes: int) -> None:
        """PUT /v1/archive：请求体从 src 流式读（Content-Length 是 size）。"""
        await self._stream(
            endpoint, "PUT", f"/v1/archive?max_bytes={_positive(max_bytes)}", src, int(size), {"Content-Type": "application/x-tar"},
            lambda resp: resp.read(),
        )

    async def manifest(self, endpoint: AgentEndpoint) -> list[FileEntry]:
        status, body = await self._call(endpoint, "GET", "/v1/manifest", None, {}, 600)
        self.raise_for(status, body)
        try:
            entries = [FileEntry.from_dict(e) for e in json.loads(body)["entries"]]
        except (ValueError, KeyError, TypeError):
            raise TransientError("sandbox_bad_response", "沙箱返回的清单格式不对") from None
        return sorted(entries, key=lambda e: e.path)

    async def list_dir(self, endpoint: AgentEndpoint, path: str = "") -> list[DirEntry]:
        suffix = "/" + _quote(check_path(path)) if path not in ("", ".") else ""
        status, body = await self._call(endpoint, "GET", "/v1/list" + suffix, None, {})
        self.raise_for(status, body)
        try:
            return [DirEntry(name=str(e["name"]), type=str(e["type"]), size=int(e.get("size") or 0)) for e in json.loads(body)["entries"]]
        except (ValueError, KeyError, TypeError):
            raise TransientError("sandbox_bad_response", "沙箱返回的目录格式不对") from None

    async def grep(self, endpoint: AgentEndpoint, pattern: str, *, path: str = "", max_matches: int = 100) -> tuple[list[GrepMatch], bool]:
        if path not in ("", "."):
            check_path(path)
        body = json.dumps({"pattern": pattern, "path": "" if path == "." else path, "max_matches": int(max_matches)}).encode()
        status, data = await self._call(endpoint, "POST", "/v1/grep", body, {"Content-Type": "application/json"}, 300)
        self.raise_for(status, data)
        try:
            out = json.loads(data)
            matches = [GrepMatch(path=str(m["path"]), line=int(m["line"]), text=str(m["text"])) for m in out["matches"]]
            return matches, bool(out["truncated"])
        except (ValueError, KeyError, TypeError):
            raise TransientError("sandbox_bad_response", "沙箱返回的搜索结果格式不对") from None

    async def _stream(
        self, endpoint: AgentEndpoint, method: str, path: str, body: IO[bytes] | None, length: int, headers: Mapping[str, str],
        receive: Callable[[Any], T],
    ) -> T:
        """请求体与响应体都不整个放进内存的调用；出错时按 raise_for 报。超时按十分钟算（几个 GB 的工作区）。"""
        request = urllib.request.Request(endpoint.base_url + path, data=body, method=method)
        for k, v in {**endpoint.headers, **headers, TOKEN_HEADER: endpoint.token}.items():
            request.add_header(k, v)
        if body is not None:
            request.add_header("Content-Length", str(length))

        def send() -> T:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=endpoint.ssl_context))
            try:
                with opener.open(request, timeout=600) as resp:
                    return receive(resp)
            except urllib.error.HTTPError as err:
                self.raise_for(err.code, err.read())
                raise TransientError("sandbox_unavailable", f"沙箱暂时不可用（HTTP {err.code}）") from None
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError) as err:
                raise TransientError("sandbox_unavailable", f"沙箱的传输中断了（{type(err).__name__}）") from None

        return await asyncio.to_thread(send)

    async def healthy(self, endpoint: AgentEndpoint) -> bool:
        try:
            status, _ = await self._call(endpoint, "GET", "/healthz", None, {}, 5)
        except TransientError:
            return False
        return status == 200

    async def _call(
        self, endpoint: AgentEndpoint, method: str, path: str, body: bytes | None, headers: Mapping[str, str], timeout: float | None = None
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(endpoint.base_url + path, data=body, method=method)
        for k, v in {**endpoint.headers, **headers, TOKEN_HEADER: endpoint.token}.items():
            request.add_header(k, v)
        limit = timeout if timeout is not None else self._request_timeout.total_seconds()
        connection = _Connection()

        def send() -> tuple[int, bytes]:
            opener = connection.opener(endpoint.ssl_context)
            try:
                with opener.open(request, timeout=limit) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as err:
                return err.code, err.read()
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError) as err:
                raise TransientError("sandbox_unavailable", f"连不上沙箱（{type(err).__name__}）") from None

        running = asyncio.ensure_future(asyncio.to_thread(send))
        try:
            return await asyncio.shield(running)
        except asyncio.CancelledError:
            connection.abort()
            # 线程随之出错返回，没有人要它的结果。
            running.add_done_callback(lambda t: t.cancelled() or t.exception())
            raise

    @staticmethod
    def raise_for(status: int, body: bytes) -> None:
        """按 HTTP 状态码与 {"code", "message"} 抛相应的错误；2xx 不抛。桥（Cloudflare）的错误也用它。"""
        if 200 <= status < 300:
            return
        code, message = _error_of(body)
        error: ProviderError
        if status in (400, 409, 413, 422):
            error = InvalidError(code or "invalid", message or "沙箱拒绝了这个请求")
        elif status == 404:
            error = NotFoundError(code or "not_found", message or "不存在")
        elif status in (401, 403):
            error = DeniedError(code or "sandbox_denied", message or "没有权限访问这个沙箱")
        else:
            error = TransientError(code or "sandbox_unavailable", message or f"沙箱暂时不可用（HTTP {status}）")
        raise error


def _error_of(body: bytes) -> tuple[str, str]:
    try:
        data = json.loads(body)
    except ValueError:
        return "", ""
    if isinstance(data, dict):
        return str(data.get("code") or ""), str(data.get("message") or "")
    return "", ""


def _quote(path: str) -> str:
    return urllib.parse.quote(path, safe="/")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _unb64(value: object) -> bytes:
    if not value:
        return b""
    return base64.b64decode(str(value))
