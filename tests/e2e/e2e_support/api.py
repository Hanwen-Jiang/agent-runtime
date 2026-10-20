"""Task API 的 HTTP + JSON 客户端（connect 协议的 unary JSON）与 SSE 读取器，只用标准库。"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

SERVICE = "/agentruntime.taskapi.v1.TaskService/"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class TaskApi:
    def __init__(self, base_url: str, token: str | None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def call(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base_url + SERVICE + method, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as err:
            payload = json.loads(err.read() or b"{}")
            raise ApiError(err.code, payload.get("code", ""), payload.get("message", "")) from None

    def submit(
        self,
        message: str = "上季度退款率为什么上升？",
        *,
        session_id: str = "",
        client_request_id: str | None = None,
        stub_plan: str = "answer",
        read_only: bool = False,
        tenant_id: str = "",
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """inputs 并入 Task 的 inputs（例如故障 Worker 的 fault_stage）。"""
        body: dict[str, Any] = {
            "client_request_id": client_request_id or uuid.uuid4().hex,
            "message": message,
            "inputs": {"stub_plan": stub_plan, **(inputs or {})},
            "session_access": "SESSION_ACCESS_READ_ONLY" if read_only else "SESSION_ACCESS_READ_WRITE",
        }
        if session_id:
            body["session_id"] = session_id
        if tenant_id:
            body["tenant_id"] = tenant_id
        return self.call("SubmitTask", body)

    def cancel(self, task_id: str, reason: str = "e2e") -> dict[str, Any]:
        return self.call("CancelTask", {"task_id": task_id, "reason": reason})

    def query(self, task_id: str) -> dict[str, Any]:
        return self.call("QueryTask", {"task_id": task_id})

    def reply(self, task_id: str, question_id: str, option: str, guidance: str = "") -> dict[str, Any]:
        return self.call(
            "SignalTask",
            {"task_id": task_id, "human_reply": {"question_id": question_id, "option": option, "guidance": guidance}},
        )

    def wait_status(self, task_id: str, status: str, timeout: float = 60) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.query(task_id)
            if last.get("status") == status:
                return last
            time.sleep(0.2)
        raise TimeoutError(f"{timeout} 秒内 {task_id} 没有变成 {status}，最后是 {last}")


@dataclass
class SseEvent:
    id: int
    event: str
    data: dict[str, Any]


class SseStream:
    """GET 订阅地址，逐个读出事件。"""

    def __init__(self, url: str, token: str | None, last_event_id: int | None = None, timeout: float = 60) -> None:
        parts = urllib.parse.urlsplit(url)
        self.conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
        headers = {"Accept": "text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        self.conn.request("GET", path, headers=headers)
        self.response = self.conn.getresponse()
        self.status = self.response.status

    def __iter__(self) -> Iterator[SseEvent]:
        fields: dict[str, str] = {}
        while True:
            raw = self.response.readline()
            if not raw:
                return
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith(":"):
                continue
            if line == "":
                if "id" in fields or "data" in fields:
                    yield SseEvent(int(fields.get("id", "0")), fields.get("event", ""), json.loads(fields.get("data", "{}")))
                fields = {}
                continue
            key, _, value = line.partition(":")
            fields[key] = fields.get(key, "") + value.lstrip(" ")

    def close(self) -> None:
        self.conn.close()


def stream_until_finished(url: str, token: str, last_event_id: int | None = None, timeout: float = 90) -> list[SseEvent]:
    """读到 task_finished 为止，返回全部事件。"""
    stream = SseStream(url, token, last_event_id, timeout=timeout)
    try:
        assert stream.status == 200, f"SSE 应返回 200，实际 {stream.status}"
        events: list[SseEvent] = []
        for ev in stream:
            events.append(ev)
            if ev.event == "task_finished":
                return events
        raise AssertionError(f"推流在 task_finished 之前结束：{events}")
    finally:
        stream.close()


def finished_payload(events: list[SseEvent]) -> dict[str, Any]:
    """task_finished 事件的载荷，即 Task 的结局（TaskWorkflowResult 的 JSON）。"""
    finished = [e for e in events if e.event == "task_finished"]
    assert len(finished) == 1, f"应恰好有一个 task_finished：{events}"
    return finished[0].data["payload"]["value"]
