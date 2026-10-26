"""测试里的 Cloudflare 沙箱桥（docs/plan/cloud-deploy.md D-07）：与 deploy/cloudflare/sandbox-bridge 同一套 HTTP 接口，
每个环境起一个真的 sandbox-agent 进程（本机编译），转发规则也相同：核对桥的令牌与 X-Sandbox-Owner，agent 的令牌只在桥里。

用来跑 Cloudflare 适配器的一致性测试；Cloudflare 那边的东西（microVM、出网白名单、空闲回收）测不到，真实端点的用例要设
AR_LIVE_CLOUDFLARE=1（花钱、不进 CI）。要 PATH 上的 go。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
_ENV_PATH = re.compile(r"^/v1/sandboxes/([^/]+)(/agent(/.*))?$")
_ENV_ID = re.compile(r"^cfs-[0-9a-f]{16}$")


class MissingTool(RuntimeError):
    pass


@dataclass
class _Env:
    owner: str
    proc: subprocess.Popen[bytes]
    port: int
    token: str
    workdir: Path
    allow_hosts: list[str] = field(default_factory=list)
    idle_seconds: int = 600


class FakeBridge:
    def __init__(self, token: str | None = None) -> None:
        go = shutil.which("go")
        if go is None:
            raise MissingTool("找不到 go（要编译 sandbox-agent）")
        self.token = token or secrets.token_hex(24)
        self._agent_key = secrets.token_bytes(32)
        self._dir = Path(tempfile.mkdtemp(prefix="ar-cfbridge-"))
        self._agent = self._dir / ("sandbox-agent" + (".exe" if os.name == "nt" else ""))
        result = subprocess.run(
            [go, "build", "-o", str(self._agent), "./cmd/sandbox-agent"],
            cwd=REPO_ROOT / "services" / "go-sandbox-agent", capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
        )
        if result.returncode != 0:
            raise RuntimeError(f"编译 sandbox-agent 失败：{result.stderr}")
        self.envs: dict[str, _Env] = {}
        self.created: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                bridge.handle(self)

            def do_POST(self) -> None:  # noqa: N802
                bridge.handle(self)

            def do_PUT(self) -> None:  # noqa: N802
                bridge.handle(self)

            def do_DELETE(self) -> None:  # noqa: N802
                bridge.handle(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    # --- 与 sandbox-bridge 相同的路由 ---

    def handle(self, h: BaseHTTPRequestHandler) -> None:
        if h.path == "/healthz":
            return self._send(h, 200, b"ok")
        want = f"Bearer {self.token}"
        if not hmac.compare_digest(h.headers.get("Authorization", ""), want):
            return self._problem(h, 401, "unauthorized")
        owner = h.headers.get("X-Sandbox-Owner", "")
        if not re.match(r"^(_platform|[a-z0-9][a-z0-9_-]{0,62})$", owner):
            return self._problem(h, 400, "invalid_owner")
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else b""
        if h.command == "POST" and h.path == "/v1/sandboxes":
            spec = json.loads(body or b"{}")
            env_id = "cfs-" + secrets.token_hex(8)
            self._start(env_id, owner, spec)
            self.created.append({"owner": owner, **spec})
            return self._send(h, 201, json.dumps({"env_id": env_id}).encode(), "application/json")
        m = _ENV_PATH.match(h.path.split("?", 1)[0])
        if not m or not _ENV_ID.match(m.group(1)):
            return self._problem(h, 404, "unknown_env")
        env = self.envs.get(m.group(1))
        if h.command == "DELETE" and not m.group(2):
            if env is None:
                return self._send(h, 204, b"")
            if env.owner != owner:
                return self._problem(h, 404, "unknown_env")
            self._stop(m.group(1))
            return self._send(h, 204, b"")
        if env is None or env.owner != owner or not m.group(3):
            return self._problem(h, 404, "unknown_env")
        agent_path = h.path[h.path.index("/agent") + len("/agent"):]
        request = urllib.request.Request(f"http://127.0.0.1:{env.port}{agent_path}", data=body or None, method=h.command)
        request.add_header("X-Sandbox-Token", env.token)
        if h.headers.get("Content-Type"):
            request.add_header("Content-Type", h.headers["Content-Type"])
        try:
            with urllib.request.urlopen(request, timeout=600) as resp:
                return self._send(h, resp.status, resp.read(), resp.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as err:
            return self._send(h, err.code, err.read(), err.headers.get("Content-Type", ""))

    def _start(self, env_id: str, owner: str, spec: dict[str, Any]) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])
        token = hmac.new(self._agent_key, env_id.encode(), hashlib.sha256).hexdigest()
        workdir = self._dir / env_id
        env = {**os.environ, "AGENT_TOKEN": token, "AGENT_WORKDIR": str(workdir), "AGENT_ADDR": f"127.0.0.1:{port}"}
        proc = subprocess.Popen([str(self._agent)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1):
                    break
            except OSError:
                if time.monotonic() > deadline or proc.poll() is not None:
                    proc.kill()
                    raise RuntimeError("sandbox-agent 没有起来") from None
                time.sleep(0.05)
        with self._lock:
            self.envs[env_id] = _Env(owner, proc, port, token, workdir, list(spec.get("allow_hosts") or []), int(spec.get("idle_seconds") or 600))

    def _stop(self, env_id: str) -> None:
        with self._lock:
            env = self.envs.pop(env_id, None)
        if env is not None:
            env.proc.kill()
            env.proc.wait(timeout=10)
            shutil.rmtree(env.workdir, ignore_errors=True)

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, status: int, body: bytes, content_type: str = "text/plain") -> None:
        h.send_response(status)
        if content_type:
            h.send_header("Content-Type", content_type)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if body:
            h.wfile.write(body)

    def _problem(self, h: BaseHTTPRequestHandler, status: int, code: str) -> None:
        self._send(h, status, json.dumps({"code": code, "message": code}).encode(), "application/json")

    def stop(self) -> None:
        for env_id in list(self.envs):
            self._stop(env_id)
        self._server.shutdown()
        self._server.server_close()
        shutil.rmtree(self._dir, ignore_errors=True)
