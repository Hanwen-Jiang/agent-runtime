"""PUT、GET /v1/files（D-28 黑盒；对应 Go 版的文件用例）。"""

from __future__ import annotations

import os
from pathlib import Path

from conftest import Agent, b64


def test_files_roundtrip_and_are_visible_to_commands(agent: Agent) -> None:
    res = agent.request("PUT", "/v1/files/data/input.txt", "退款".encode())
    assert res.status == 204 and res.body == b""
    res = agent.request("GET", "/v1/files/data/input.txt")
    assert res.status == 200 and res.body == "退款".encode()
    assert res.headers["content-type"] == "application/octet-stream" and res.headers["content-length"] == str(len("退款".encode()))
    assert b64(agent.run("cat", "data/input.txt").json()["stdout"]) == "退款".encode(), "命令在工作目录里看得到文件"
    agent.run("write", "out.txt", "from-command")
    assert agent.request("GET", "/v1/files/out.txt").body == b"from-command"


def test_writing_replaces_and_empty_files_work(agent: Agent) -> None:
    assert agent.request("PUT", "/v1/files/a.txt", b"long content").status == 204
    assert agent.request("PUT", "/v1/files/a.txt", b"x").status == 204
    assert agent.request("GET", "/v1/files/a.txt").body == b"x"
    assert agent.request("PUT", "/v1/files/empty", b"").status == 204
    res = agent.request("GET", "/v1/files/empty")
    assert res.status == 200 and res.body == b""


def test_percent_encoded_names(agent: Agent) -> None:
    assert agent.request("PUT", "/v1/files/dir%20one/%E9%80%80%E6%AC%BE.txt", b"x").status == 204
    assert (agent.workdir / "dir one" / "退款.txt").read_bytes() == b"x"
    assert agent.request("GET", "/v1/files/dir%20one/%E9%80%80%E6%AC%BE.txt").body == b"x"


def test_paths_cannot_escape_the_workdir(agent: Agent) -> None:
    outside = agent.workdir.parent / "outside.txt"
    outside.write_text("secret", "utf-8")
    for p in ["../outside.txt", "a/../../outside.txt", "..%2Foutside.txt", "%2E%2E/outside.txt", "./a", "a//b", "a/", "C:/Windows/x",
              "%2Fetc%2Fpasswd", "a/%2E%2E/%2E%2E/outside.txt", "%00", "a%00b"]:
        agent.request("PUT", "/v1/files/" + p, b"PWNED")
        res = agent.request("GET", "/v1/files/" + p)
        assert res.body != b"secret", p
    assert outside.read_text("utf-8") == "secret", "工作目录外的文件被改了"
    for p in ["a%5Cb", "C:x", "..%2Foutside.txt", "a%00b", "%2Fetc"]:
        res = agent.request("PUT", "/v1/files/" + p, b"x")
        assert res.status == 400 and res.code == "invalid_path", (p, res.status, res.body)


def test_unclean_paths_are_redirected_like_go(agent: Agent) -> None:
    # ServeMux 的规范化：非规范的路径 301 到规范的（GET 与 PUT 都一样）；agent 自己不再处理 . 与 ..。
    for p in ["/v1/files/a//b", "/v1/files/./a", "/v1/files/a/../b"]:
        res = agent.request("GET", p)
        assert res.status == 301, (p, res.status)
        assert res.headers["location"] in ("/v1/files/a/b", "/v1/files/a", "/v1/files/b")


def test_missing_files_and_directories_are_not_found(agent: Agent) -> None:
    (agent.workdir / "sub").mkdir()
    for p in ["missing.txt", "sub", "missing/deeper.txt"]:
        res = agent.request("GET", "/v1/files/" + p)
        assert res.status == 404 and res.code == "not_found", p


def test_a_file_where_a_directory_is_expected(agent: Agent) -> None:
    (agent.workdir / "f").write_text("x", "utf-8")
    res = agent.request("PUT", "/v1/files/f/g.txt", b"y")
    assert res.status == 400 and res.code == "invalid_path"
    res = agent.request("GET", "/v1/files/f/g.txt")
    assert res.status in (404, 500)


def test_file_size_limit(agent: Agent) -> None:
    res = agent.request("PUT", "/v1/files/big.bin", b"\0" * 5000)
    assert res.status == 413 and res.code == "too_large"
    assert not (agent.workdir / "big.bin").exists()
    assert agent.request("PUT", "/v1/files/max.bin", b"\0" * 4096).status == 204, "正好等于上限可以"


def test_reads_follow_the_workdir_not_the_agent_cwd(agent: Agent) -> None:
    assert os.getcwd() != str(agent.workdir)
    (agent.workdir / "here.txt").write_text("here", "utf-8")
    assert agent.request("GET", "/v1/files/here.txt").body == b"here"
    assert not Path("here.txt").exists()
