"""sandbox-agent 的客户端（agent 在 services/go-sandbox-agent）。K8s 的沙箱 Pod 与 Cloudflare 的 Container 里跑的是同一个 agent，
Sandbox 的这两个适配器都经它执行命令、读写文件（计划 docs/plan/cloud-deploy.md 的 D-05、D-07）。只用标准库。

怎么连到 agent 由适配器给出（AgentEndpoint）：集群内直连 Pod 的地址；集群外经 K8s API Server 的 pods/proxy；
Cloudflare 经桥（Worker）转发。agent 的令牌放 X-Sandbox-Token 头，额外的头（API Server、桥的认证）另放。

错误：agent 的 400、413 是 invalid；404 是 not_found；401、403 是 denied（令牌或权限配错了）；连不上、5xx、429 是 transient。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError, ProviderError, TransientError
from agentruntime_core.providers.sandbox import ExecResult

TOKEN_HEADER = "X-Sandbox-Token"
_DRIVE = re.compile(r"^[A-Za-z]:")


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


class AgentClient:
    def __init__(self, *, request_timeout: timedelta = timedelta(seconds=60)) -> None:
        self._request_timeout = request_timeout

    async def exec(self, endpoint: AgentEndpoint, argv: Sequence[str], *, timeout: timedelta, stdin: bytes = b"") -> ExecResult:
        if not argv:
            raise InvalidError("invalid_argument", "argv 不能为空")
        ms = max(1, int(timeout.total_seconds() * 1000))
        body = json.dumps({"argv": list(argv), "stdin": _b64(stdin), "timeout_ms": ms}).encode()
        status, data = await self._call(
            endpoint, "POST", "/v1/exec", body, {"Content-Type": "application/json"}, timeout.total_seconds() + self._request_timeout.total_seconds()
        )
        self.raise_for(status, data)
        try:
            out = json.loads(data)
            return ExecResult(
                exit_code=out["exit_code"], stdout=_unb64(out.get("stdout")), stderr=_unb64(out.get("stderr")), timed_out=bool(out["timed_out"])
            )
        except (ValueError, KeyError, TypeError):
            raise TransientError("sandbox_bad_response", "沙箱返回的结果格式不对") from None

    async def write_file(self, endpoint: AgentEndpoint, path: str, data: bytes) -> None:
        status, body = await self._call(endpoint, "PUT", "/v1/files/" + _quote(check_path(path)), data, {"Content-Type": "application/octet-stream"})
        self.raise_for(status, body)

    async def read_file(self, endpoint: AgentEndpoint, path: str) -> bytes:
        status, body = await self._call(endpoint, "GET", "/v1/files/" + _quote(check_path(path)), None, {})
        self.raise_for(status, body)
        return body

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

        def send() -> tuple[int, bytes]:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=endpoint.ssl_context))
            try:
                with opener.open(request, timeout=limit) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as err:
                return err.code, err.read()
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
                raise TransientError("sandbox_unavailable", f"连不上沙箱（{type(err).__name__}）") from None

        return await asyncio.to_thread(send)

    @staticmethod
    def raise_for(status: int, body: bytes) -> None:
        """按 HTTP 状态码与 {"code", "message"} 抛相应的错误；2xx 不抛。桥（Cloudflare）的错误也用它。"""
        if 200 <= status < 300:
            return
        code, message = _error_of(body)
        error: ProviderError
        if status in (400, 413, 422):
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
