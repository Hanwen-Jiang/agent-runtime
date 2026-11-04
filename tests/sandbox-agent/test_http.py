"""认证、路由、启动参数与 HTTP/1.1 的细节（D-28 黑盒；Go 版的 TestConfigNeedsALongToken、TestHealthNeedsNoTokenButEverythingElseDoes）。"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import pytest
from conftest import TOKEN, Agent, start_agent

PROTECTED = [
    ("POST", "/v1/exec"), ("POST", "/v1/exec/cancel"), ("PUT", "/v1/files/a.txt"), ("GET", "/v1/files/a.txt"),
    ("GET", "/v1/archive?max_bytes=1"), ("PUT", "/v1/archive?max_bytes=1"), ("GET", "/v1/manifest"), ("GET", "/v1/list"),
    ("GET", "/v1/list/sub"), ("POST", "/v1/grep"),
]


def test_health_needs_no_token_but_everything_else_does(agent: Agent) -> None:
    res = agent.request("GET", "/healthz", token=None)
    assert res.status == 200 and res.body == b"ok"
    for token in (None, "", "wrong-token-wrong-token-wrong-token", TOKEN + "x", TOKEN[:-1]):
        for method, path in PROTECTED:
            res = agent.request(method, path, b"{}", token=token)
            assert res.status == 401, (method, path, token, res.status)
            assert res.json() == {"code": "unauthorized", "message": "令牌不对"}
            assert res.headers["content-type"] == "application/json"


def test_the_token_header_is_case_insensitive_and_trimmed(agent: Agent) -> None:
    res = agent.request("GET", "/v1/manifest", token=None, headers={"x-sandbox-token": "  " + TOKEN + " "})
    assert res.status == 200


def test_unknown_routes_and_methods(agent: Agent) -> None:
    assert agent.request("GET", "/nope").status == 404
    assert agent.request("GET", "/v1/exec").status == 405
    res = agent.request("DELETE", "/v1/files/a.txt")
    assert res.status == 405 and "GET" in res.headers.get("allow", "") and "PUT" in res.headers.get("allow", "")


@pytest.mark.parametrize("token", ["short", ""])
def test_the_agent_refuses_to_start_without_a_long_token(exe: Path, tmp_path: Path, token: str) -> None:
    env = {"AGENT_TOKEN": token, "AGENT_ADDR": "127.0.0.1:0", "AGENT_WORKDIR": str(tmp_path / "w"), "PATH": os.environ.get("PATH", "")}
    res = subprocess.run([str(exe)], env=env, capture_output=True, timeout=20)
    assert res.returncode != 0


def test_the_token_can_come_from_a_file(exe: Path, tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text(TOKEN + "\n", "utf-8")
    a = start_agent(exe, tmp_path / "w", extra_env={"AGENT_TOKEN": None, "AGENT_TOKEN_FILE": str(f)})  # type: ignore[dict-item]
    try:
        assert a.request("GET", "/v1/manifest").status == 200
    finally:
        a.proc.kill()
        a.proc.wait()


@pytest.mark.parametrize("name,value", [("AGENT_OUTPUT_LIMIT", "0"), ("AGENT_FILE_LIMIT", "-1"), ("AGENT_MAX_TIMEOUT_S", "abc")])
def test_bad_numeric_settings_refuse_to_start(exe: Path, tmp_path: Path, name: str, value: str) -> None:
    env = {"AGENT_TOKEN": TOKEN, "AGENT_ADDR": "127.0.0.1:0", "AGENT_WORKDIR": str(tmp_path / "w"), name: value}
    res = subprocess.run([str(exe)], env=env, capture_output=True, timeout=20)
    assert res.returncode != 0


def test_version_and_install(exe: Path, tmp_path: Path) -> None:
    res = subprocess.run([str(exe), "version"], capture_output=True, text=True, timeout=20)
    assert res.returncode == 0 and res.stdout.strip() == "0.1.0"
    dest = tmp_path / "deep" / "dir" / ("agent" + (".exe" if os.name == "nt" else ""))
    res = subprocess.run([str(exe), "install", str(dest)], capture_output=True, timeout=20)
    assert res.returncode == 0, res.stderr
    assert dest.read_bytes() == exe.read_bytes()
    if os.name != "nt":
        assert dest.stat().st_mode & 0o111
    assert subprocess.run([str(exe), "install"], capture_output=True, timeout=20).returncode != 0
    assert subprocess.run([str(exe), "bogus"], capture_output=True, timeout=20).returncode != 0


def test_the_workdir_is_created(make_agent, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    wd = tmp_path / "a" / "b" / "c"
    make_agent(workdir=wd)
    assert wd.is_dir()


def _raw(agent: Agent, data: bytes, *, read_until_close: bool = False, timeout: float = 10) -> bytes:
    with socket.create_connection(("127.0.0.1", agent.port), timeout=timeout) as s:
        s.sendall(data)
        if read_until_close:
            s.shutdown(socket.SHUT_WR)
        out = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = s.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            out += chunk
            if not read_until_close and out.count(b"HTTP/1.1 ") >= data.count(b"HTTP/1.1\r\n") and out.endswith(b"\n"):
                # 足够多的响应头都到了；给剩下的一点时间
                s.settimeout(0.5)
        return out


def test_keep_alive_serves_several_requests_on_one_connection(agent: Agent) -> None:
    conn = agent.connect()
    try:
        for i in range(3):
            conn.request("PUT", f"/v1/files/k{i}.txt", body=f"v{i}".encode(), headers={"X-Sandbox-Token": TOKEN})
            r = conn.getresponse()
            r.read()
            assert r.status == 204
        conn.request("GET", "/v1/files/k2.txt", headers={"X-Sandbox-Token": TOKEN})
        r = conn.getresponse()
        assert r.status == 200 and r.read() == b"v2"
    finally:
        conn.close()


def test_chunked_request_bodies_are_accepted(agent: Agent) -> None:
    body = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
    req = (f"PUT /v1/files/chunked.txt HTTP/1.1\r\nHost: x\r\nX-Sandbox-Token: {TOKEN}\r\nTransfer-Encoding: chunked\r\n"
           "Connection: close\r\n\r\n").encode() + body
    out = _raw(agent, req, read_until_close=True)
    assert out.startswith(b"HTTP/1.1 204"), out
    assert agent.request("GET", "/v1/files/chunked.txt").body == b"hello world"


def test_expect_continue_gets_a_100_before_the_body(agent: Agent) -> None:
    with socket.create_connection(("127.0.0.1", agent.port), timeout=10) as s:
        s.sendall((f"PUT /v1/files/c.txt HTTP/1.1\r\nHost: x\r\nX-Sandbox-Token: {TOKEN}\r\nContent-Length: 3\r\n"
                   "Expect: 100-continue\r\nConnection: close\r\n\r\n").encode())
        first = s.recv(4096)
        assert first.startswith(b"HTTP/1.1 100"), first
        s.sendall(b"abc")
        rest = b""
        while chunk := s.recv(4096):
            rest += chunk
        assert (first + rest).split(b"\r\n\r\n", 1)[1].startswith(b"HTTP/1.1 204"), first + rest
    assert agent.request("GET", "/v1/files/c.txt").body == b"abc"


def test_head_on_healthz(agent: Agent) -> None:
    res = agent.request("HEAD", "/healthz", token=None)
    assert res.status == 200 and res.body == b""


def test_garbage_requests_get_400_and_do_not_kill_the_agent(agent: Agent) -> None:
    out = _raw(agent, b"NOT HTTP AT ALL\r\n\r\n", read_until_close=True)
    assert out.startswith(b"HTTP/1.1 400"), out
    out = _raw(agent, f"PUT /v1/files/x HTTP/1.1\r\nHost: x\r\nX-Sandbox-Token: {TOKEN}\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\nab".encode(),
               read_until_close=True)
    assert out.startswith(b"HTTP/1.1 400"), out
    assert agent.request("GET", "/healthz", token=None).status == 200
