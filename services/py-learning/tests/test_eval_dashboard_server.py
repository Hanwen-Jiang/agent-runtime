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


@pytest.mark.parametrize("path", ["/", "/index.html"])
def test_the_page_is_served_with_security_headers(site: tuple[int, Path], path: str) -> None:
    port, root = site
    (root / "index.html").write_text(PAGE, "utf-8")
    status, headers, body = request(port, "GET", path)
    assert status == 200 and body.decode("utf-8") == PAGE
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert headers["content-security-policy"] == CSP, "页面只有内联的脚本与样式，只许连回同源取实时数据（第 11.16 节）"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "public, max-age=300", "Cloudflare 可以短暂缓存，发布后几分钟内更新"


def test_head_has_the_headers_and_no_body(site: tuple[int, Path]) -> None:
    port, root = site
    (root / "index.html").write_text(PAGE, "utf-8")
    status, headers, body = request(port, "HEAD", "/")
    assert status == 200 and body == b""
    assert headers["content-length"] == str(len(PAGE.encode("utf-8")))


def test_healthz_answers_before_anything_is_published(site: tuple[int, Path]) -> None:
    port, _ = site
    status, _, body = request(port, "GET", "/healthz")
    assert status == 200 and body == b"ok"
    status, _, _ = request(port, "GET", "/")
    assert status == 404, "还没发布时没有页面"


def test_a_republished_page_shows_up_without_a_restart(site: tuple[int, Path]) -> None:
    port, root = site
    (root / "index.html").write_text("first", "utf-8")
    assert request(port, "GET", "/")[2] == b"first"
    (root / "index.html").write_text("second", "utf-8")
    assert request(port, "GET", "/")[2] == b"second"


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


def test_a_query_string_does_not_change_what_is_served(site: tuple[int, Path]) -> None:
    port, root = site
    (root / "index.html").write_text(PAGE, "utf-8")
    status, _, body = request(port, "GET", "/?x=1")
    assert status == 200 and body.decode("utf-8") == PAGE


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


def test_python_dash_m_starts_the_server_the_chart_uses(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(PAGE, "utf-8")
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentruntime_learning.evals.dashboard_server", "--dir", str(tmp_path), "--port", str(port), "--host", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                status, _, body = request(port, "GET", "/")
                break
            except OSError:
                assert time.monotonic() < deadline, "服务没有起来"
                time.sleep(0.1)
        assert status == 200 and body.decode("utf-8") == PAGE
    finally:
        proc.terminate()
        proc.wait(timeout=10)
