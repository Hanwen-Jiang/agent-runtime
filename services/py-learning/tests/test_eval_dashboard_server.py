"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 11.12 节：评测看板（Eval Dashboard）的静态服务。

agentruntime_learning.evals.dashboard_server 只用标准库，跑在已有的 learning 镜像里：只答 /、/index.html 与 /healthz（第 11.16 节另加
/api/live.json，见 test_dashboard_live.py），只收 GET、
HEAD，带安全响应头，每次请求现读文件（ConfigMap 更新后不用重启），不回显请求内容，不露版本。目录里的别的文件一概不给。
"""

from __future__ import annotations

import http.client
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentruntime_learning.evals.dashboard_server import make_server

PAGE = "<!doctype html><title>Eval Dashboard · 评测看板</title>"
CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


@pytest.fixture
def site(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    root = tmp_path / "site"
    root.mkdir()
    server = make_server(root, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], root
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(port: int, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_accept_encoding=True)
        conn.endheaders()
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "path",
    ["/secret.txt", "/../secret.txt", "/%2e%2e/secret.txt", "/index.html/../secret.txt", "//etc/passwd", "/index.html.bak", "/.git/config"],
)
def test_nothing_else_is_served(site: tuple[int, Path], path: str) -> None:
    port, root = site
    (root / "index.html").write_text(PAGE, "utf-8")
    (root / "secret.txt").write_text("do-not-serve", "utf-8")
    status, _, body = request(port, "GET", path)
    assert status == 404
    assert b"do-not-serve" not in body


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_only_get_and_head_are_accepted(site: tuple[int, Path], method: str) -> None:
    port, root = site
    (root / "index.html").write_text(PAGE, "utf-8")
    status, headers, _ = request(port, method, "/")
    assert status == 405
    assert headers["allow"] == "GET, HEAD"


def test_errors_do_not_echo_the_request_or_reveal_the_version(site: tuple[int, Path]) -> None:
    port, _ = site
    status, headers, body = request(port, "GET", "/<script>alert(1)</script>")
    assert status == 404
    assert b"alert(1)" not in body and b"<script>" not in body
    assert "python" not in headers.get("server", "").lower()
    assert "python" not in body.decode("utf-8", "replace").lower()


def test_the_module_stays_light() -> None:
    code = "import sys, agentruntime_learning.evals.dashboard_server as m; heavy = {'temporalio', 'grpc', 'agentruntime_worker', 'agentruntime_core'} & set(sys.modules); assert not heavy, heavy"
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


