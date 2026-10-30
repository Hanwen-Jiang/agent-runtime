"""TMP 计划 eval-observability-and-runtime-fixes 第 11.10 节：SWE-bench 适配器的 HTTP 一侧，对着本机的假服务测。

1. Task API 与 Workspace API 走 connect 协议的 unary JSON：路径是 /<服务>/<方法>，带 Bearer 令牌；请求用 proto 的字段名，
   回应里驼峰的字段名、字符串形式的 int64（花费是 micros）换算成适配器用的形状；没给的 bool 当 false。
2. 订阅地址只取路径，按 Task API 的地址补全：task-service 没配 AR_PUBLIC_BASE_URL 时给的是自己的监听地址（线上是
   http://0.0.0.0:8080/…），从别处连不上；SSE 带 Last-Event-ID 续读；事件的载荷取 payload.value。
3. 出错时是 ApiError（HTTP 状态与 connect 的 code）；错误信息里没有令牌。
4. fetch：按 datasets-server 的 rows 接口分页取全；条数对不上、有被截断的格子时报错；写出来的是 LF 的 JSONL。
5. 调 Runtime 的请求（含 SSE）带自己的 User-Agent：线上的 Task API 在 Cloudflare 后面，它拒绝 Python-urllib 的默认值（错误 1010）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from agentruntime_learning.benchmarks import swebench as sb
from agentruntime_learning.benchmarks.client import ApiError, Event, RuntimeClient

TOKEN = "tok-SECRET-0123456789"
TASK = "/agentruntime.taskapi.v1.TaskService/"
WORKSPACE = "/agentruntime.workspace.v1.WorkspaceService/"


def sse(event_id: int, name: str, value: dict[str, Any]) -> bytes:
    data = {"event_id": event_id, "task_id": "t-1", "type": "TASK_EVENT_TYPE_" + name.upper(), "payload": {"value": value}}
    return f"id: {event_id}\nevent: {name}\ndata: {json.dumps(data)}\n\n".encode()


class Fake:
    """记下每个请求（路径、头（名字小写，HTTP 头不分大小写）、JSON 体），按路径给回应。"""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.rows: list[dict[str, Any]] = []
        self.rows_total: int | None = None
        self.truncated_cells: list[str] = []
        self.events_url = "/v1/tasks/t-1/events"

    def unary(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        method = path.rsplit("/", 1)[-1]
        if method == "CreateWorkspace":
            return 200, {"workspace": {"workspaceId": body["workspace_id"], "head": "abc123", "state": "WORKSPACE_STATE_ACTIVE"}}
        if method == "GetDiff":
            return 200, {"diff": "diff --git a/a b/a\n", "files": ["a"], "head": "abc123"}
        if method == "DeleteWorkspace":
            return (404, {"code": "not_found", "message": "工作区 w-gone 不存在"}) if body["workspace_id"] == "w-gone" else (200, {})
        if method == "SubmitTask":
            return 200, {"taskId": "t-1", "sessionId": "ses-1", "eventsUrl": self.events_url}
        if method == "QueryTask":
            return 200, {"taskId": "t-1", "status": "TASK_STATUS_RUNNING", "roundSeq": 2, "usage": {"inputTokens": "10", "costUsdMicros": "1250000"}}
        if method in ("SignalTask", "CancelTask"):
            return 200, {}
        return 404, {"code": "unimplemented", "message": method}

    def events(self, last: int) -> bytes:
        all_events = [
            sse(1, "task_accepted", {}),
            sse(2, "verdict", {"verdict": "continue", "reason": "还差测试"}),
            sse(3, "task_finished", {"task_id": "t-1", "status": "TASK_STATUS_DONE", "final_verdict": "VERDICT_DONE", "rounds": 2,
                                     "usage": {"cost_usd_micros": "420000"}}),
        ]
        # 第一次连接推两个就断；带 Last-Event-ID 的推剩下的。
        return b": ping\n\n" + b"".join(all_events[last:last + 2] if last == 0 else all_events[last:])


@pytest.fixture
def server() -> Iterator[tuple[str, Fake]]:
    fake = Fake()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _record(self, body: Any) -> None:
            fake.requests.append({"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.loads(raw or b"{}")
            self._record(body)
            status, out = fake.unary(self.path, body)
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._record(None)
            parts = urlsplit(self.path)
            if parts.path == "/rows":
                q = {k: v[0] for k, v in parse_qs(parts.query).items()}
                offset, length = int(q["offset"]), int(q["length"])
                page = [{"row_idx": i, "row": r, "truncated_cells": fake.truncated_cells} for i, r in enumerate(fake.rows[offset:offset + length], offset)]
                data = json.dumps({"rows": page, "num_rows_total": fake.rows_total if fake.rows_total is not None else len(fake.rows)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            last = int(self.headers.get("Last-Event-ID") or 0)
            data = fake.events(last)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", fake
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_workspace_calls(server: tuple[str, Fake]) -> None:
    base, fake = server
    c = RuntimeClient(base, TOKEN)
    files = [{"path": ".devcontainer/devcontainer.json", "content": "{}\n"}]
    created = c.create_workspace("w-1", repo_url="https://github.com/acme/calc.git", ref="a" * 40, files=files)
    assert created == {"workspace_id": "w-1", "head": "abc123", "branch": ""}
    req = fake.requests[-1]
    assert req["path"] == WORKSPACE + "CreateWorkspace" and req["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert req["body"] == {"workspace_id": "w-1", "repo_url": "https://github.com/acme/calc.git", "ref": "a" * 40, "files": files}

    assert c.diff("w-1", base="abc123") == {"diff": "diff --git a/a b/a\n", "files": ["a"], "truncated": False}, "没给的 bool 当 false"
    assert fake.requests[-1]["body"] == {"workspace_id": "w-1", "base": "abc123"}
    c.delete_workspace("w-1")
    assert fake.requests[-1]["path"] == WORKSPACE + "DeleteWorkspace" and fake.requests[-1]["body"] == {"workspace_id": "w-1"}


def test_task_calls(server: tuple[str, Fake]) -> None:
    base, fake = server
    c = RuntimeClient(base, TOKEN)
    sub = c.submit(workspace_id="w-1", message="修 add()", client_request_id="cr-1")
    assert sub == {"task_id": "t-1", "events_url": f"{base}/v1/tasks/t-1/events"}, "相对的订阅地址按 Task API 的地址补全"
    fake.events_url = "http://0.0.0.0:8080/v1/tasks/t-1/events"
    sub = c.submit(workspace_id="w-1", message="修 add()", client_request_id="cr-1")
    assert sub["events_url"] == f"{base}/v1/tasks/t-1/events", "绝对的订阅地址（没配公开地址时是监听地址）也只取路径"
    assert fake.requests[-1]["path"] == TASK + "SubmitTask"
    assert fake.requests[-1]["body"] == {"workspace_id": "w-1", "message": "修 add()", "client_request_id": "cr-1", "session_access": "SESSION_ACCESS_READ_WRITE"}

    assert c.query("t-1") == {"status": "TASK_STATUS_RUNNING", "cost_usd": 1.25}, "int64 是字符串，micros 换成美元"
    c.reply("t-1", "q-1", "stop_and_wrap_up")
    assert fake.requests[-1]["path"] == TASK + "SignalTask"
    assert fake.requests[-1]["body"] == {"task_id": "t-1", "human_reply": {"question_id": "q-1", "option": "stop_and_wrap_up"}}
    c.cancel("t-1", "task_usd_limit")
    assert fake.requests[-1]["body"] == {"task_id": "t-1", "reason": "task_usd_limit"}


def test_events_resume_with_last_event_id(server: tuple[str, Fake]) -> None:
    base, fake = server
    c = RuntimeClient(base, TOKEN)
    url = f"{base}/v1/tasks/t-1/events"
    first = list(c.events("t-1", url))
    assert first == [Event(1, "task_accepted", {}), Event(2, "verdict", {"verdict": "continue", "reason": "还差测试"})]
    assert "last-event-id" not in fake.requests[-1]["headers"]
    rest = list(c.events("t-1", url, last_event_id=2))
    assert fake.requests[-1]["headers"]["last-event-id"] == "2" and fake.requests[-1]["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert [e.name for e in rest] == ["task_finished"] and rest[0].id == 3
    assert rest[0].value["usage"]["cost_usd_micros"] == "420000" and rest[0].value["final_verdict"] == "VERDICT_DONE"


def test_errors_carry_the_code_but_not_the_token(server: tuple[str, Fake]) -> None:
    base, _ = server
    c = RuntimeClient(base, TOKEN)
    with pytest.raises(ApiError) as info:
        c.delete_workspace("w-gone")
    assert (info.value.status, info.value.code) == (404, "not_found")
    assert TOKEN not in str(info.value) and TOKEN not in repr(info.value)


def test_requests_carry_their_own_user_agent(server: tuple[str, Fake]) -> None:
    base, fake = server
    c = RuntimeClient(base, TOKEN)
    with pytest.raises(ApiError):
        c.delete_workspace("w-gone")
    list(c.events("t-1", f"{base}/v1/tasks/t-1/events"))
    agents = [r["headers"].get("user-agent", "") for r in fake.requests]
    assert len(agents) == 2 and all(a.startswith("agentruntime-benchmarks/") for a in agents), agents


def test_fetch_pages_through_the_rows_api(server: tuple[str, Fake], tmp_path: Path) -> None:
    base, fake = server
    fake.rows = [{"instance_id": f"acme__calc-{i}", "repo": "acme/calc"} for i in range(5)]
    out = tmp_path / "instances.jsonl"
    count = sb.fetch_instances(out, dataset="SWE-bench/SWE-bench_Verified", split="test", rows_url=f"{base}/rows", page_size=2)
    assert count == 5
    text = out.read_bytes().decode("utf-8")
    assert "\r" not in text and [json.loads(line)["instance_id"] for line in text.splitlines()] == [r["instance_id"] for r in fake.rows]
    pages = [parse_qs(urlsplit(r["path"]).query) for r in fake.requests if r["path"].startswith("/rows")]
    assert [p["offset"][0] for p in pages] == ["0", "2", "4"] and {p["length"][0] for p in pages} == {"2"}
    assert {p["dataset"][0] for p in pages} == {"SWE-bench/SWE-bench_Verified"} and {p["split"][0] for p in pages} == {"test"}


def test_fetch_refuses_incomplete_data(server: tuple[str, Fake], tmp_path: Path) -> None:
    base, fake = server
    fake.rows = [{"instance_id": "acme__calc-1"}]
    fake.rows_total = 2
    with pytest.raises(ValueError, match="2"):
        sb.fetch_instances(tmp_path / "a.jsonl", rows_url=f"{base}/rows", page_size=2)
    fake.rows_total = None
    fake.truncated_cells = ["test_patch"]
    with pytest.raises(ValueError, match="test_patch"):
        sb.fetch_instances(tmp_path / "b.jsonl", rows_url=f"{base}/rows", page_size=2)
