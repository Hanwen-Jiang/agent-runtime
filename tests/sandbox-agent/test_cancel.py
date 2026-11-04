"""调用方断开与 POST /v1/exec/cancel（D-28 黑盒；对应 Go 版 disconnect_test.go，docs/plan/running-task-interaction.md 第 2 节）。

Windows 上没有进程组，只看直接的进程。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

from conftest import HELPER, TOKEN, Agent


def _wait_for_file(path: str) -> None:
    deadline = time.monotonic() + 10
    while not (os.path.exists(path) and os.path.getsize(path) > 0):
        assert time.monotonic() < deadline, f"没有等到 {path}"
        time.sleep(0.02)


def _still_growing(path: str) -> bool:
    before = os.path.getsize(path)
    time.sleep(1)
    return os.path.getsize(path) > before


def _exec_body(mode: str, marker: str, exec_id: str | None = None) -> bytes:
    req: dict[str, object] = {"argv": [sys.executable, HELPER, mode, marker], "timeout_ms": 25000, "stdin": base64.b64encode(b"").decode()}
    if exec_id:
        req["exec_id"] = exec_id
    return json.dumps(req).encode()


def test_a_client_that_disconnects_stops_the_command_and_its_children(agent: Agent, tmp_path: Path) -> None:
    marker = str(tmp_path / "beat")
    mode = "tree" if sys.platform == "linux" else "beat"
    body = _exec_body(mode, marker)
    s = socket.create_connection(("127.0.0.1", agent.port), timeout=30)
    s.sendall(f"POST /v1/exec HTTP/1.1\r\nHost: x\r\nX-Sandbox-Token: {TOKEN}\r\nContent-Type: application/json\r\n"
              f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    _wait_for_file(marker)
    if mode == "tree":
        _wait_for_file(marker + ".child")
    s.close()
    time.sleep(0.5)
    assert not _still_growing(marker), "调用方断开了，命令还在跑"
    if mode == "tree":
        assert not _still_growing(marker + ".child"), "调用方断开了，命令的子进程还在跑"
    assert agent.request("GET", "/healthz", token=None).status == 200


def test_a_half_closed_client_also_counts_as_gone(agent: Agent, tmp_path: Path) -> None:
    # 只关写的一半（FIN）：Go 的服务端读到 EOF 就当调用方走了。
    marker = str(tmp_path / "beat")
    body = _exec_body("beat", marker)
    s = socket.create_connection(("127.0.0.1", agent.port), timeout=30)
    s.sendall(f"POST /v1/exec HTTP/1.1\r\nHost: x\r\nX-Sandbox-Token: {TOKEN}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
    _wait_for_file(marker)
    s.shutdown(socket.SHUT_WR)
    time.sleep(0.5)
    try:
        assert not _still_growing(marker)
    finally:
        s.close()


def test_the_cancel_endpoint_stops_the_command(agent: Agent, tmp_path: Path) -> None:
    marker = str(tmp_path / "beat")
    status: list[int] = []
    t = threading.Thread(target=lambda: status.append(agent.request("POST", "/v1/exec", _exec_body("tree", marker, "e1"), timeout=30).status))
    t.start()
    _wait_for_file(marker)
    res = agent.request("POST", "/v1/exec/cancel", b'{"exec_id":"e1"}')
    assert res.status == 204 and res.body == b""
    t.join(10)
    assert not t.is_alive(), "取消之后请求没有结束"
    assert status == [409]
    assert not _still_growing(marker), "取消之后命令还在跑"
    if sys.platform == "linux" and os.path.exists(marker + ".child"):
        assert not _still_growing(marker + ".child")


def test_a_cancelled_exec_answers_409_with_a_code(agent: Agent, tmp_path: Path) -> None:
    marker = str(tmp_path / "beat")
    out: list[object] = []
    t = threading.Thread(target=lambda: out.append(agent.request("POST", "/v1/exec", _exec_body("beat", marker, "e9"), timeout=30)))
    t.start()
    _wait_for_file(marker)
    agent.request("POST", "/v1/exec/cancel", b'{"exec_id":"e9"}')
    t.join(10)
    res = out[0]
    assert res.status == 409 and res.json() == {"code": "exec_cancelled", "message": "这条命令已经取消了"}  # type: ignore[attr-defined]


def test_a_cancel_that_arrives_first_stops_the_command_from_starting(agent: Agent, tmp_path: Path) -> None:
    marker = str(tmp_path / "beat")
    assert agent.request("POST", "/v1/exec/cancel", b'{"exec_id":"e2"}').status == 204
    res = agent.request("POST", "/v1/exec", _exec_body("beat", marker, "e2"))
    assert res.status == 409 and res.code == "exec_cancelled"
    assert not os.path.exists(marker), "已经取消的命令不该开始"
    # 先到的取消只用一次：同一个 exec_id 再来就照常跑。
    res = agent.request("POST", "/v1/exec", json.dumps({"argv": [sys.executable, HELPER, "print", "ok"], "timeout_ms": 5000, "exec_id": "e2"}).encode())
    assert res.status == 200


def test_cancel_needs_an_exec_id(agent: Agent) -> None:
    for body in (b"{}", b"", b"nope", b'{"exec_id": ""}', b'{"exec_id": 5}'):
        res = agent.request("POST", "/v1/exec/cancel", body)
        assert res.status == 400 and res.code == "invalid_argument", body


def test_cancelling_an_unknown_or_finished_exec_is_harmless(agent: Agent) -> None:
    res = agent.request("POST", "/v1/exec", json.dumps({"argv": [sys.executable, HELPER, "print", "x"], "timeout_ms": 5000, "exec_id": "done"}).encode())
    assert res.status == 200
    assert agent.request("POST", "/v1/exec/cancel", b'{"exec_id":"done"}').status == 204
    assert agent.request("POST", "/v1/exec/cancel", b'{"exec_id":"never"}').status == 204
