"""POST /v1/exec（D-28 黑盒；对应 Go 版 agent_test.go 的命令用例）。"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from conftest import HELPER, Agent, b64, linux_only


def test_exec_returns_output_and_exit_code(agent: Agent) -> None:
    res = agent.run("print", "42")
    assert res.status == 200, res.body
    out = res.json()
    assert out["exit_code"] == 0 and b64(out["stdout"]) == b"42" and out["timed_out"] is False and out["truncated"] is False
    assert b64(out["stderr"]) == b"" and isinstance(out["cpu_ms"], int)
    assert set(out) == {"exit_code", "timed_out", "stdout", "stderr", "truncated", "cpu_ms"}
    out = agent.run("fail", "3", "bad").json()
    assert out["exit_code"] == 3 and b64(out["stderr"]) == b"bad", "非零退出码照常返回"


def test_exec_reports_cpu_time_for_metering(agent: Agent) -> None:
    busy = agent.run("burn", "0.3").json()
    idle = agent.run("sleep", "0.3").json()
    assert busy["cpu_ms"] >= 100 and idle["cpu_ms"] <= busy["cpu_ms"], (busy["cpu_ms"], idle["cpu_ms"])


def test_exec_passes_stdin(agent: Agent) -> None:
    assert b64(agent.run("echo-upper", stdin=b"abc").json()["stdout"]) == b"ABC"
    assert b64(agent.run("echo-upper").json()["stdout"]) == b"", "没有 stdin 时命令读到 EOF"


def test_stdout_and_stderr_are_separate(agent: Agent) -> None:
    out = agent.run("both", "10").json()
    assert b64(out["stdout"]) == b"o" * 10 and b64(out["stderr"]) == b"e" * 10


def test_exec_timeout_is_reported_not_raised(agent: Agent) -> None:
    start = time.monotonic()
    res = agent.run("sleep", "30", timeout_ms=500)
    assert res.status == 200
    out = res.json()
    assert out["timed_out"] is True and out["exit_code"] is None
    assert time.monotonic() - start < 10, "超时后要尽快返回"


@linux_only
def test_timeout_kills_the_whole_process_group(agent: Agent, tmp_path: Path) -> None:
    marker = str(tmp_path / "beat")
    out = agent.run("tree", marker, timeout_ms=1500).json()
    assert out["timed_out"] is True
    time.sleep(0.3)
    assert not _still_growing(marker) and not _still_growing(marker + ".child")


def test_output_keeps_the_tail(agent: Agent) -> None:
    out = agent.run("spam", "5000").json()
    data = b64(out["stdout"])
    assert out["truncated"] is True and len(data) == 1024 and data.endswith(b"END")


def test_output_exactly_at_the_limit_is_not_truncated(agent: Agent) -> None:
    out = agent.run("spam", str(1024 - 3)).json()
    assert out["truncated"] is False and len(b64(out["stdout"])) == 1024


@linux_only
def test_killed_by_a_signal_is_a_negative_exit_code(agent: Agent) -> None:
    out = agent.run("signal", "SIGTERM").json()
    assert out["exit_code"] == -15 and out["timed_out"] is False


@linux_only
def test_a_background_process_holding_the_pipes_does_not_hang_the_request(agent: Agent) -> None:
    start = time.monotonic()
    out = agent.run("orphan", "20", timeout_ms=30000).json()
    assert out["exit_code"] == 0 and b64(out["stdout"]) == b"parent-done"
    assert time.monotonic() - start < 10, "进程退出后最多再等 WaitDelay（2 秒）收尾"


def test_commands_run_in_the_workdir(agent: Agent) -> None:
    cwd = b64(agent.run("cwd").json()["stdout"]).decode()
    assert os.path.samefile(cwd, agent.workdir)


def test_commands_only_see_the_configured_environment(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent({"AGENT_ENV_PASSTHROUGH": "VISIBLE, GOCACHE AGENT_TOKEN agent_addr bad-name", "GOCACHE": "/tmp/go"})
    names = set(json.loads(b64(agent.run("env-names").json()["stdout"])))
    assert not any(n.upper().startswith("AGENT_") for n in names), names
    assert "SECRET_NOT_LISTED" not in names
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR", "PATHEXT", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE",
               "VISIBLE", "GOCACHE"}
    extra = names - allowed
    # Windows 与 macOS 的进程会自己加几个（例如 __CF_USER_TEXT_ENCODING、=C:）；Linux 上一个都不多。
    if sys.platform == "linux":
        assert not extra, extra
    assert b64(agent.run("env", "VISIBLE").json()["stdout"]) == b"yes"
    assert b64(agent.run("env", "GOCACHE").json()["stdout"]) == b"/tmp/go"
    assert b64(agent.run("env", "AGENT_TOKEN").json()["stdout"]) == b"<unset>"
    assert b64(agent.run("env", "PYTHONIOENCODING").json()["stdout"]) == b"utf-8"
    assert b64(agent.run("env", "PYTHONDONTWRITEBYTECODE").json()["stdout"]) == b"1"


@linux_only
def test_home_and_lang_have_defaults(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent({"HOME": None, "LANG": None})
    assert b64(agent.run("env", "HOME").json()["stdout"]) == b"/tmp"
    assert b64(agent.run("env", "LANG").json()["stdout"]) == b"C.UTF-8"
    agent = make_agent({"HOME": "/home/x", "LANG": "zh_CN.UTF-8", "TZ": "Asia/Shanghai"})
    assert b64(agent.run("env", "HOME").json()["stdout"]) == b"/home/x"
    assert b64(agent.run("env", "LANG").json()["stdout"]) == b"zh_CN.UTF-8"
    assert b64(agent.run("env", "TZ").json()["stdout"]) == b"Asia/Shanghai"


def test_request_env_is_added_and_cannot_touch_agent_variables(agent: Agent) -> None:
    def run(name: str, env: dict[str, str] | None):  # type: ignore[no-untyped-def]
        return agent.run("env", name, env=env)

    assert b64(run("PIP_INDEX_URL", {"PIP_INDEX_URL": "http://mirror/pypi/simple/"}).json()["stdout"]) == b"http://mirror/pypi/simple/"
    assert b64(run("VISIBLE", {"VISIBLE": "replaced"}).json()["stdout"]) == b"replaced", "同名的以请求为准"
    assert b64(run("PIP_INDEX_URL", None).json()["stdout"]) == b"<unset>", "上一次请求的 env 不留到下一次"
    assert b64(run("EMPTY", {"EMPTY": ""}).json()["stdout"]) == b""
    for env in ({"AGENT_TOKEN": "x"}, {"agent_addr": "x"}, {"Agent_X": "x"}, {"BAD-NAME": "x"}, {"1A": "x"}, {"": "x"}, {"A": "x\x00y"}):
        res = run("A", env)
        assert res.status == 400 and res.code == "invalid_env", (env, res.status, res.body)
    many = {f"V{i}": "x" for i in range(65)}
    assert run("V1", many).status == 400, "最多 64 个"
    assert run("V1", {f"V{i}": "x" for i in range(64)}).status == 200
    big = {"BIG": "x" * (64 << 10)}
    assert run("BIG", big).status in (400, 413), "总共最多 64 KiB"


@linux_only
def test_bare_names_are_looked_up_on_the_commands_path(agent: Agent, tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    tool = tools / "only-here"
    tool.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write('found:' + sys.argv[0])\n", "utf-8")
    tool.chmod(0o755)
    res = agent.exec(["only-here"])
    assert res.status == 400 and res.code == "command_not_found", "agent 的 PATH 里没有它"
    out = agent.exec(["only-here"], env={"PATH": str(tools)}).json()
    assert out["exit_code"] == 0 and b64(out["stdout"]).startswith(b"found:")
    (tools / "not-exec").write_text("x", "utf-8")
    assert agent.exec(["not-exec"], env={"PATH": str(tools)}).code == "command_not_found", "没有执行权限的不算"
    assert agent.exec([str(tools / "only-here")]).json()["exit_code"] == 0, "带路径的照原样用"


def test_limits_and_bad_requests(agent: Agent) -> None:
    res = agent.exec(["no-such-program-xyz"], timeout_ms=1000)
    assert res.status == 400 and res.code == "command_not_found"
    for argv in ([], [""]):
        res = agent.exec(argv, timeout_ms=1000)
        assert res.status == 400 and res.code == "invalid_argument"
    for t in (120_000, 0, -1):
        res = agent.run("print", "x", timeout_ms=t)
        assert res.status == 400 and res.code == "invalid_timeout", t
    assert agent.run("print", "x", timeout_ms=60_000).status == 200, "正好等于上限可以"
    res = agent.request("POST", "/v1/exec", b"not json")
    assert res.status == 400 and res.code == "invalid_request"
    res = agent.request("POST", "/v1/exec", b'{"argv": "ls", "timeout_ms": 1000}')
    assert res.status == 400 and res.code == "invalid_request", "类型不对"
    res = agent.request("POST", "/v1/exec", b'{"argv": ["x"], "timeout_ms": 1.5}')
    assert res.status == 400 and res.code == "invalid_request"
    res = agent.request("POST", "/v1/exec", b'{"argv": ["x"], "timeout_ms": 1000, "stdin": "!!!"}')
    assert res.status == 400 and res.code == "invalid_request", "stdin 不是 base64"
    res = agent.request("POST", "/v1/exec", b"null")
    assert res.status == 400 and res.code == "invalid_argument"
    res = agent.run("echo-upper", stdin=b"x" * 4097)
    assert res.status == 413 and res.code == "too_large", "stdin 超过文件上限"
    assert agent.run("echo-upper", stdin=b"x" * 4096).status == 200
    res = agent.request("POST", "/v1/exec", b'{"argv": ["x"], "pad": "' + b"x" * 20000 + b'"}')
    assert res.status == 413 and res.code == "too_large", "请求体超过上限"


def test_json_field_names_are_matched_like_go(agent: Agent) -> None:
    body = json.dumps({"ARGV": [sys.executable, HELPER, "print", "ci"], "Timeout_MS": 5000}).encode()
    res = agent.request("POST", "/v1/exec", body)
    assert res.status == 200 and b64(res.json()["stdout"]) == b"ci"
    body = json.dumps({"argv": [sys.executable, HELPER, "print", "ok"], "timeout_ms": 5000, "unknown": 1}).encode() + b" trailing"
    assert agent.request("POST", "/v1/exec", body).status == 200, "只读第一个 JSON 值，未知字段忽略"


def test_responses_are_json_with_a_trailing_newline(agent: Agent) -> None:
    res = agent.run("print", "<&>")
    assert res.headers["content-type"] == "application/json" and res.body.endswith(b"}\n")
    res = agent.exec([], timeout_ms=1)
    assert res.body.endswith(b"}\n") and res.headers["content-type"] == "application/json"


def _still_growing(path: str) -> bool:
    try:
        before = os.path.getsize(path)
    except OSError:
        return False
    time.sleep(1)
    return os.path.getsize(path) > before
