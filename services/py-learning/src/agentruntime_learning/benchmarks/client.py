"""Task API 与 Workspace API 的 HTTP 客户端：connect 协议的 unary JSON 与 SSE，只用标准库（TMP 计划 11.10）。

- 请求体用 proto 的字段名（下划线）；回应是 connect 的 JSON（驼峰的字段名，int64 是字符串），这里换成适配器用的形状。
- SSE 的事件按 task-service 的写法：id、event（TaskEventType 去掉前缀的小写名）与 data（TaskEvent 的 protojson，载荷在
  payload.value）。一次 events() 只读一条连接，断了就结束；续读由调用方带 Last-Event-ID 再调。
- 令牌只放在 Authorization 头里，不进错误信息。
- 请求带自己的 User-Agent：线上的 Task API 在 Cloudflare 后面，它的 Browser Integrity Check 拒绝 Python-urllib 的默认值（错误 1010）。
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TASK_SERVICE = "/agentruntime.taskapi.v1.TaskService/"
WORKSPACE_SERVICE = "/agentruntime.workspace.v1.WorkspaceService/"
ENVIRONMENT_SERVICE = "/agentruntime.environment.v1.EnvironmentService/"
USER_AGENT = "agentruntime-benchmarks/1"


class ApiError(Exception):
    """Task API 或 Workspace API 的错误：HTTP 状态与 connect 的 code（连不上时 status 为 0、code 为 unavailable）。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


@dataclass
class Event:
    """SSE 的一个事件：id、名字（task_finished 之类）与载荷（TaskEvent.payload.value）。"""

    id: int
    name: str
    value: dict[str, Any]


def _micros(value: Any) -> float:
    return int(value or 0) / 1_000_000


def token_from_env() -> str | Callable[[], str]:
    """Task API only: reread a rotated ops JWT file for every HTTP/SSE request.

    The issuer remains an operator concern (ops-token.sh); never generate dev
    tokens or forward this credential to Memory/Model Gateway.
    """
    path = os.environ.get("AR_TASK_API_TOKEN_FILE", "").strip()
    if path:
        def read() -> str:
            try:
                token = Path(path).read_text("utf-8").strip()
            except OSError:
                raise ValueError("cannot read AR_TASK_API_TOKEN_FILE") from None
            if not token or any(c.isspace() for c in token):
                raise ValueError("AR_TASK_API_TOKEN_FILE must contain one token")
            return token
        read()  # fail before creating a workspace
        return read
    return os.environ.get("AR_TASK_API_TOKEN", "").strip()


class RuntimeClient:
    def __init__(self, base_url: str, token: str | Callable[[], str], *, timeout: float = 60, events_timeout: float = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        # SSE 的读超时：task-service 定时发心跳，超过这么久没有任何字节就当断了，由调用方续读。
        self.events_timeout = events_timeout

    def __repr__(self) -> str:
        return f"RuntimeClient({self.base_url!r})"

    def _headers(self, **extra: str) -> dict[str, str]:
        token = self._token() if callable(self._token) else self._token
        return {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT, **extra}

    def _call(self, service: str, method: str, body: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + service + method, data=json.dumps(body).encode(), method="POST",
            headers=self._headers(**{"Content-Type": "application/json"}),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as err:
            try:
                payload = json.loads(err.read() or b"{}")
            except ValueError:
                payload = {}
            raise ApiError(err.code, str(payload.get("code") or "unknown"), str(payload.get("message") or err.reason)) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            raise ApiError(0, "unavailable", f"{method} 连不上 {self.base_url}：{getattr(err, 'reason', err)}") from None

    # --- Workspace API ---

    def create_workspace(self, workspace_id: str, *, repo_url: str, ref: str, files: list[dict[str, str]]) -> dict[str, Any]:
        # 克隆大仓库要一会儿：等得比普通调用久（task-service 那边默认等 600 秒）。
        out = self._call(WORKSPACE_SERVICE, "CreateWorkspace", {"workspace_id": workspace_id, "repo_url": repo_url, "ref": ref, "files": files},
                         timeout=max(self.timeout, 660))
        ws = out.get("workspace") or {}
        return {"workspace_id": str(ws.get("workspaceId") or workspace_id), "head": str(ws.get("head") or ""), "branch": str(ws.get("branch") or "")}

    def delete_workspace(self, workspace_id: str) -> None:
        self._call(WORKSPACE_SERVICE, "DeleteWorkspace", {"workspace_id": workspace_id})

    def diff(self, workspace_id: str, *, base: str) -> dict[str, Any]:
        out = self._call(WORKSPACE_SERVICE, "GetDiff", {"workspace_id": workspace_id, "base": base}, timeout=max(self.timeout, 660))
        return {"diff": str(out.get("diff") or ""), "files": [str(f) for f in out.get("files") or []], "truncated": bool(out.get("truncated", False))}

    # --- Task API ---

    def submit(self, *, workspace_id: str, message: str, client_request_id: str, network: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """network 是提交时的联网申请（Task API 的 NetworkRequest：hosts、minutes、reason；EV-TB-NET），没有时不带。"""
        body: dict[str, Any] = {
            "workspace_id": workspace_id, "message": message, "client_request_id": client_request_id,
            "session_access": "SESSION_ACCESS_READ_WRITE",
        }
        if network:
            body["network_request"] = {"hosts": list(network["hosts"]), "minutes": int(network["minutes"]), "reason": str(network.get("reason") or "")}
        out = self._call(TASK_SERVICE, "SubmitTask", body)
        # 订阅地址只取路径，按 Task API 的地址补全：SSE 与 unary 是同一个 task-service、同一个端口，而它没配
        # AR_PUBLIC_BASE_URL 时给的是自己的监听地址（线上是 http://0.0.0.0:8080/…），从别处连不上。
        parts = urllib.parse.urlsplit(str(out["eventsUrl"]))
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        return {"task_id": str(out["taskId"]), "events_url": urllib.parse.urljoin(self.base_url + "/", path), "session_id": str(out.get("sessionId") or "")}

    def query(self, task_id: str) -> dict[str, Any]:
        out = self._call(TASK_SERVICE, "QueryTask", {"task_id": task_id})
        usage = out.get("usage") or {}
        return {"status": str(out.get("status") or ""), "cost_usd": _micros(usage.get("costUsdMicros"))}

    def reply(self, task_id: str, question_id: str, option: str) -> None:
        self._call(TASK_SERVICE, "SignalTask", {"task_id": task_id, "human_reply": {"question_id": question_id, "option": option}})

    def cancel(self, task_id: str, reason: str) -> None:
        self._call(TASK_SERVICE, "CancelTask", {"task_id": task_id, "reason": reason})

    # --- Environment API（沙箱环境表的只读投影）---

    def environments(self, workspace_id: str, session_id: str) -> list[dict[str, Any]]:
        """这个工作区、Session 的沙箱环境（含已销毁的），每个给 image、state、runtime（EV-TB-2：看用上项目环境没有）。"""
        out = self._call(ENVIRONMENT_SERVICE, "ListEnvironments", {"workspace_id": workspace_id, "session_id": session_id, "limit": 100})
        return [{"image": str(e.get("image") or ""), "state": str(e.get("state") or ""), "runtime": str(e.get("runtime") or "")}
                for e in out.get("environments") or []]

    def events(self, task_id: str, events_url: str, *, last_event_id: int = 0) -> Iterator[Event]:
        """读一条 SSE 连接上的事件，连接断了（含读超时）就结束。"""
        headers = self._headers(Accept="text/event-stream")
        if last_event_id:
            headers["Last-Event-ID"] = str(last_event_id)
        request = urllib.request.Request(events_url, headers=headers, method="GET")
        try:
            resp = urllib.request.urlopen(request, timeout=self.events_timeout)
        except urllib.error.HTTPError as err:
            try:
                payload = json.loads(err.read() or b"{}")
            except ValueError:
                payload = {}
            raise ApiError(err.code, str(payload.get("code") or "unknown"), str(payload.get("message") or f"订阅 {task_id} 的事件失败")) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            raise ApiError(0, "unavailable", f"订阅 {task_id} 的事件连不上：{getattr(err, 'reason', err)}") from None
        with resp:
            fields: dict[str, str] = {}
            while True:
                try:
                    raw = resp.readline()
                except (TimeoutError, ConnectionError, http.client.IncompleteRead, OSError):
                    return
                if not raw:
                    return
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith(":"):
                    continue
                if line:
                    key, _, value = line.partition(":")
                    fields[key] = fields.get(key, "") + value.removeprefix(" ")
                    continue
                if "data" in fields:
                    data = json.loads(fields["data"])
                    event_id = int(fields.get("id") or data.get("event_id") or 0)
                    value = (data.get("payload") or {}).get("value") or {}
                    yield Event(event_id, fields.get("event", ""), dict(value))
                fields = {}
