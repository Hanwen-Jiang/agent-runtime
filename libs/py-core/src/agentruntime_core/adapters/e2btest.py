"""测试里的 E2B（e2b 分支，docs/plan/e2b-backend.md）：api 与 client-proxy 各一个本机 HTTP 服务，接口与固定版本的 E2B
（提交 67b3182 的 spec/openapi.yml 与 envd 的 process.proto、envd.yaml）相同的那一部分：

- api：X-API-Key；POST /v2/sandboxes（含 Idempotency-Key 的重放）、GET /sandboxes/{id}、GET /v2/sandboxes（metadata 与
  state 过滤、分页）、DELETE、POST …/timeout、…/pause、/v2/…/connect；模板构建（POST /v3/templates、
  POST /v2/templates/{id}/builds/{build}、GET …/status）。
- client-proxy + envd：按头路由（E2b-Sandbox-Id、E2b-Sandbox-Port 必须是 49983），X-Access-Token 要对；/files 的读写；
  process.Process 的 Start（Connect 的服务端流，每个环境的命令是本机的真实子进程）、SendInput、CloseStdin、SendSignal。
  沙箱暂停时 envd 不可达（502）。

E2B 那边的东西（Firecracker、快照恢复、出网规则）测不到；真实的自托管 E2B 设 AR_LIVE_E2B=1 跑（要 KVM 节点）。
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import tempfile
import threading
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from agentruntime_core.adapters.envd import ENVD_PORT

HOME = "/home/user"
_SANDBOX = re.compile(r"^/sandboxes/([a-z0-9]+)(/[a-z]+)?$")
_CONNECT = re.compile(r"^/v2/sandboxes/([a-z0-9]+)/connect$")
_BUILD = re.compile(r"^/v2/templates/([a-z0-9-]+)/builds/([a-z0-9-]+)$")
_STATUS = re.compile(r"^/templates/([a-z0-9-]+)/builds/([a-z0-9-]+)/status$")
# 子进程只带这些环境变量（存在时），与 envd 在 VM 里给的差不多：能找到 python。
_PASS_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "LANG")


@dataclass
class FakeSandbox:
    sandbox_id: str
    template: str
    metadata: dict[str, str]
    network: dict[str, Any]
    allow_internet: bool | None
    timeout: int
    auto_pause: bool
    envd_token: str
    root: Path
    state: str = "running"
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    procs: dict[int, subprocess.Popen[bytes]] = field(default_factory=dict)
    timeouts: list[int] = field(default_factory=list)


class FakeE2B:
    def __init__(self, api_key: str | None = None, *, templates: tuple[str, ...] = ("agentruntime-python",)) -> None:
        self.api_key = api_key or "e2b_" + secrets.token_hex(20)
        self.templates: set[str] = set(templates)
        self.sandboxes: dict[str, FakeSandbox] = {}
        self.calls: list[tuple[str, str]] = []
        self.creates: list[dict[str, Any]] = []
        self.builds: list[dict[str, Any]] = []
        self.page_size = 100
        self._idempotency: dict[str, tuple[str, int, bytes]] = {}
        self._template_ids: dict[str, str] = {}
        self._dir = Path(tempfile.mkdtemp(prefix="ar-e2b-"))
        self._lock = threading.Lock()
        fake = self

        class Api(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                fake.api(self)

            do_POST = do_DELETE = do_GET  # noqa: N815

        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                fake.proxy(self)

            do_POST = do_GET  # noqa: N815

        self._api = ThreadingHTTPServer(("127.0.0.1", 0), Api)
        self._proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        self.api_url = f"http://127.0.0.1:{self._api.server_address[1]}"
        self.sandbox_url = f"http://127.0.0.1:{self._proxy.server_address[1]}"
        for server in (self._api, self._proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    # --- api ---

    def api(self, h: BaseHTTPRequestHandler) -> None:
        path, _, query = h.path.partition("?")
        self.calls.append((h.command, path))
        if h.headers.get("X-API-Key", "") != self.api_key:
            return self._error(h, 401, "Invalid API key")
        body = self._body(h)
        if h.command == "POST" and path == "/v2/sandboxes":
            return self._create(h, body)
        if h.command == "GET" and path == "/v2/sandboxes":
            return self._list(h, urllib.parse.parse_qs(query))
        if h.command == "POST" and path == "/v3/templates":
            return self._template(h, body)
        m = _BUILD.match(path)
        if m and h.command == "POST":
            self.builds.append({"template_id": m.group(1), "build_id": m.group(2), **json.loads(body or b"{}")})
            return self._send(h, 202, b"")
        m = _STATUS.match(path)
        if m and h.command == "GET":
            return self._json(h, 200, {"templateID": m.group(1), "buildID": m.group(2), "status": "ready", "logs": [], "logEntries": []})
        m = _CONNECT.match(path)
        if m and h.command == "POST":
            sbx = self.sandboxes.get(m.group(1))
            if sbx is None:
                return self._error(h, 404, "sandbox not found")
            resumed = sbx.state == "paused"
            sbx.state = "running"
            sbx.timeouts.append(int(json.loads(body or b"{}").get("timeout") or 300))
            return self._json(h, 201 if resumed else 200, self._sandbox(sbx))
        m = _SANDBOX.match(path)
        if not m:
            return self._error(h, 404, "not found")
        sbx = self.sandboxes.get(m.group(1))
        if sbx is None:
            return self._error(h, 404, f"sandbox {m.group(1)} not found")
        action = m.group(2) or ""
        if h.command == "GET" and not action:
            return self._json(h, 200, self._detail(sbx))
        if h.command == "DELETE" and not action:
            self.kill(sbx.sandbox_id)
            return self._send(h, 204, b"")
        if h.command == "POST" and action == "/timeout":
            sbx.timeouts.append(int(json.loads(body or b"{}")["timeout"]))
            return self._send(h, 204, b"")
        if h.command == "POST" and action == "/pause":
            if sbx.state == "paused":
                return self._error(h, 409, "sandbox already paused")
            sbx.state = "paused"
            return self._send(h, 204, b"")
        return self._error(h, 404, "not found")

    def _create(self, h: BaseHTTPRequestHandler, body: bytes) -> None:
        key = h.headers.get("Idempotency-Key", "")
        if key:
            with self._lock:
                seen = self._idempotency.get(key)
            if seen is not None:
                if seen[0] != body.decode():
                    return self._error(h, 409, "idempotency_request_mismatch")
                return self._send(h, seen[1], seen[2], "application/json")
        req = json.loads(body or b"{}")
        if req.get("templateID") not in self.templates:
            return self._error(h, 404, f"template '{req.get('templateID')}' not found")
        sandbox_id = "i" + secrets.token_hex(10)
        root = self._dir / sandbox_id
        (root / HOME.strip("/")).mkdir(parents=True)
        sbx = FakeSandbox(
            sandbox_id=sandbox_id, template=req["templateID"], metadata=dict(req.get("metadata") or {}), network=dict(req.get("network") or {}),
            allow_internet=req.get("allow_internet_access"), timeout=int(req.get("timeout") or 300), auto_pause=bool(req.get("autoPause")),
            envd_token=secrets.token_hex(16), root=root,
        )
        self.sandboxes[sandbox_id] = sbx
        self.creates.append(req)
        out = json.dumps(self._sandbox(sbx)).encode()
        if key:
            with self._lock:
                self._idempotency[key] = (body.decode(), 201, out)
        return self._send(h, 201, out, "application/json")

    def _list(self, h: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        wanted: dict[str, str] = {}
        for pair in (query.get("metadata") or [""])[0].split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                wanted[urllib.parse.unquote(k)] = urllib.parse.unquote(v)
        states = set(",".join(query.get("state") or ["running,paused"]).split(","))
        items = [
            self._detail(s, listed=True) for s in self.sandboxes.values()
            if s.state in states and all(s.metadata.get(k) == v for k, v in wanted.items())
        ]
        start = int((query.get("nextToken") or ["0"])[0])
        limit = min(int((query.get("limit") or [str(self.page_size)])[0]), self.page_size)
        page = items[start : start + limit]
        headers = {"X-Next-Token": str(start + limit)} if start + limit < len(items) else {}
        return self._send(h, 200, json.dumps(page).encode(), "application/json", headers)

    def _template(self, h: BaseHTTPRequestHandler, body: bytes) -> None:
        req = json.loads(body or b"{}")
        name = str(req.get("name") or "")
        template_id = self._template_ids.setdefault(name, secrets.token_hex(8))
        self.templates.add(name)
        build_id = secrets.token_hex(8)
        self.builds.append({"template": name, "template_id": template_id, "build_id": build_id, **req})
        return self._json(h, 202, {"templateID": template_id, "buildID": build_id, "public": False, "names": [name], "tags": [], "aliases": [name]})

    def _sandbox(self, sbx: FakeSandbox) -> dict[str, Any]:
        return {"templateID": sbx.template, "sandboxID": sbx.sandbox_id, "clientID": "fake", "envdVersion": "0.9.0", "envdAccessToken": sbx.envd_token,
                "trafficAccessToken": secrets.token_hex(8), "domain": None}

    def _detail(self, sbx: FakeSandbox, *, listed: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "templateID": sbx.template, "sandboxID": sbx.sandbox_id, "clientID": "fake", "startedAt": sbx.started_at.isoformat().replace("+00:00", "Z"),
            "endAt": (sbx.started_at + timedelta(seconds=sbx.timeout)).isoformat().replace("+00:00", "Z"), "cpuCount": 2, "memoryMB": 1024,
            "diskSizeMB": 4096, "metadata": sbx.metadata, "state": sbx.state, "envdVersion": "0.9.0",
        }
        if not listed:
            out["envdAccessToken"] = sbx.envd_token
        return out

    # --- client-proxy 与 envd ---

    def proxy(self, h: BaseHTTPRequestHandler) -> None:
        sandbox_id = h.headers.get("E2b-Sandbox-Id", "")
        if h.headers.get("E2b-Sandbox-Port", "") != str(ENVD_PORT):
            return self._error(h, 400, "only the envd port is served here")
        sbx = self.sandboxes.get(sandbox_id)
        if sbx is None:
            return self._error(h, 404, "sandbox not found")
        if sbx.state != "running":
            return self._error(h, 502, "sandbox is paused")
        if h.headers.get("X-Access-Token", "") != sbx.envd_token:
            return self._error(h, 401, "invalid access token")
        path, _, query = h.path.partition("?")
        body = self._body(h)
        if path == "/files":
            return self._files(h, sbx, urllib.parse.parse_qs(query), body)
        if h.command == "POST" and path == "/process.Process/Start":
            return self._start(h, sbx, body)
        if h.command == "POST" and path.startswith("/process.Process/"):
            return self._unary(h, sbx, path.rsplit("/", 1)[1], json.loads(body or b"{}"))
        return self._error(h, 404, "not found")

    def local(self, sbx: FakeSandbox, path: str) -> Path | None:
        if not path.startswith(HOME + "/") or ".." in path.split("/"):
            return None
        return sbx.root.joinpath(*path.strip("/").split("/"))

    def _files(self, h: BaseHTTPRequestHandler, sbx: FakeSandbox, query: dict[str, list[str]], body: bytes) -> None:
        target = self.local(sbx, (query.get("path") or [""])[0])
        if target is None:
            return self._error(h, 400, "invalid path")
        if h.command == "GET":
            if target.is_dir():
                return self._error(h, 400, f"path '{target.name}' is a directory")
            if not target.is_file():
                return self._error(h, 404, "path does not exist")
            return self._send(h, 200, target.read_bytes(), "application/octet-stream")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return self._json(h, 200, [{"path": (query.get("path") or [""])[0], "name": target.name, "type": "file"}])

    def _start(self, h: BaseHTTPRequestHandler, sbx: FakeSandbox, body: bytes) -> None:
        if len(body) < 5:
            return self._error(h, 400, "bad envelope")
        _, length = struct.unpack(">BI", body[:5])
        req = json.loads(body[5 : 5 + length])
        process = req.get("process") or {}
        cwd = self.local(sbx, process.get("cwd") or HOME)
        env = {k: os.environ[k] for k in _PASS_ENV if k in os.environ}
        env.update(process.get("envs") or {})
        env["HOME"] = str(sbx.root / HOME.strip("/"))
        h.send_response(200)
        h.send_header("Content-Type", "application/connect+json")
        h.end_headers()
        try:
            proc = subprocess.Popen(
                [process.get("cmd") or "", *(process.get("args") or [])], cwd=cwd, env=env,
                stdin=subprocess.PIPE if req.get("stdin") else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError, NotADirectoryError) as err:
            message = f'error starting process: exec: "{process.get("cmd")}": executable file not found in $PATH ({type(err).__name__})'
            h.wfile.write(_frame({"error": {"code": "invalid_argument", "message": message}}, 0x02))
            return
        sbx.procs[proc.pid] = proc
        lock = threading.Lock()

        def emit(message: dict[str, Any], flags: int = 0) -> None:
            with lock:
                h.wfile.write(_frame(message, flags))
                h.wfile.flush()

        emit({"event": {"start": {"pid": proc.pid}}})

        def pump(stream: Any, name: str) -> None:
            for chunk in iter(lambda: stream.read1(65536), b""):
                emit({"event": {"data": {name: base64.b64encode(chunk).decode()}}})

        readers = [threading.Thread(target=pump, args=(proc.stdout, "stdout")), threading.Thread(target=pump, args=(proc.stderr, "stderr"))]
        for t in readers:
            t.start()
        code = proc.wait()
        for t in readers:
            t.join()
        sbx.procs.pop(proc.pid, None)
        killed = code < 0 or getattr(proc, "_killed_by_fake", False)
        end: dict[str, Any] = {"exited": not killed, "status": "signal: killed" if killed else f"exit status {code}"}
        if not killed and code:
            end["exitCode"] = code
            end["error"] = f"exit status {code}"
        emit({"event": {"end": end}})
        emit({}, 0x02)

    def _unary(self, h: BaseHTTPRequestHandler, sbx: FakeSandbox, method: str, req: dict[str, Any]) -> None:
        pid = int(((req.get("process") or {}).get("pid")) or 0)
        proc = sbx.procs.get(pid)
        if proc is None:
            return self._json(h, 404, {"code": "not_found", "message": f"process {pid} not found"})
        if method == "SendInput":
            assert proc.stdin is not None
            proc.stdin.write(base64.b64decode((req.get("input") or {}).get("stdin") or ""))
            proc.stdin.flush()
        elif method == "CloseStdin":
            assert proc.stdin is not None
            proc.stdin.close()
        elif method == "SendSignal":
            proc._killed_by_fake = True  # type: ignore[attr-defined]
            proc.kill()
        else:
            return self._json(h, 404, {"code": "unimplemented", "message": method})
        return self._json(h, 200, {})

    # --- 小工具 ---

    def kill(self, sandbox_id: str) -> None:
        sbx = self.sandboxes.pop(sandbox_id, None)
        if sbx is None:
            return
        for proc in list(sbx.procs.values()):
            proc.kill()
        shutil.rmtree(sbx.root, ignore_errors=True)

    @staticmethod
    def _body(h: BaseHTTPRequestHandler) -> bytes:
        length = int(h.headers.get("Content-Length") or 0)
        return h.rfile.read(length) if length else b""

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, status: int, body: bytes, content_type: str = "", headers: dict[str, str] | None = None) -> None:
        h.send_response(status)
        if content_type:
            h.send_header("Content-Type", content_type)
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if body:
            h.wfile.write(body)

    def _json(self, h: BaseHTTPRequestHandler, status: int, value: Any) -> None:
        self._send(h, status, json.dumps(value).encode(), "application/json")

    def _error(self, h: BaseHTTPRequestHandler, status: int, message: str) -> None:
        self._json(h, status, {"code": status, "message": message})

    def stop(self) -> None:
        for sandbox_id in list(self.sandboxes):
            self.kill(sandbox_id)
        for server in (self._api, self._proxy):
            server.shutdown()
            server.server_close()
        shutil.rmtree(self._dir, ignore_errors=True)


def _frame(message: dict[str, Any], flags: int = 0) -> bytes:
    data = json.dumps(message).encode()
    return struct.pack(">BI", flags, len(data)) + data
