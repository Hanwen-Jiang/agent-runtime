"""sandbox-agent 的黑盒测试（D-28）：同一套用例对 Go 版（services/go-sandbox-agent）与 Rust 版（services/rs-sandbox-agent）都跑。

每条用例起一个真的 agent 进程，只经 HTTP 验证。两版的二进制在会话开始时各编译一次：
- Go：services/go-sandbox-agent 下 go build；Rust：仓库根 cargo build --locked -p rs-sandbox-agent。
- AR_SANDBOX_AGENT_IMPLS（逗号分隔，默认 go,rust）挑要测的版本；AR_SANDBOX_AGENT_GO_BIN、AR_SANDBOX_AGENT_RUST_BIN 给现成的二进制。
- 缺 go 或 cargo 时失败；设 AR_ALLOW_MISSING_TOOLS=1 时跳过那一版。

被执行的命令是 helper.py（python helper.py <模式> …），不依赖 sh，Windows 上也能跑；只在 Linux 上成立的用例标了 linux_only。
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
HELPER = str(HERE / "helper.py")
TOKEN = "tok-0123456789abcdef0123456789abcdef"
EXE = ".exe" if os.name == "nt" else ""
IMPLS = [s.strip() for s in os.environ.get("AR_SANDBOX_AGENT_IMPLS", "go,rust").split(",") if s.strip()]

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="只在 Linux 上成立（进程组、信号、/proc、符号链接）")


def _missing(tool: str) -> None:
    if os.environ.get("AR_ALLOW_MISSING_TOOLS") == "1":
        pytest.skip(f"找不到 {tool}")
    pytest.fail(f"找不到 {tool}（设 AR_ALLOW_MISSING_TOOLS=1 时跳过）")


def _build(impl: str, out_dir: Path) -> Path:
    given = os.environ.get(f"AR_SANDBOX_AGENT_{impl.upper()}_BIN")
    if given:
        return Path(given)
    if impl == "go":
        go = shutil.which("go")
        if not go:
            _missing("go")
        out = out_dir / f"go-sandbox-agent{EXE}"
        cmd = [go, "build", "-trimpath", "-o", str(out), "./cmd/sandbox-agent"]
        cwd = REPO / "services" / "go-sandbox-agent"
    elif impl == "rust":
        cargo = shutil.which("cargo")
        if not cargo:
            _missing("cargo")
        cmd = [cargo, "build", "--locked", "--release", "-p", "rs-sandbox-agent"]
        cwd = REPO
        out = REPO / "target" / "release" / f"sandbox-agent{EXE}"
    else:
        raise pytest.UsageError(f"不认识的 agent 版本 {impl}")
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    if res.returncode != 0:
        pytest.fail(f"编译 {impl} 版 agent 失败：{res.stderr[-4000:]}")
    # 复制到所有人都能读、能执行的目录：以非 root 跑 agent 的用例（AGENT-TOK-01）也要能执行它。
    final = out_dir / f"{impl}-sandbox-agent{EXE}"
    if out != final:
        shutil.copy2(out, final)
    final.chmod(0o755)
    return final


@pytest.fixture(scope="session")
def bin_dir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="sandbox-agent-bin-"))
    d.chmod(0o755)
    yield d
    shutil.rmtree(d, ignore_errors=True)


_BUILT: dict[str, Path] = {}


@pytest.fixture(scope="session")
def binaries(bin_dir: Path) -> dict[str, Path]:
    return _BUILT


def binary(impl: str, bin_dir: Path) -> Path:
    if impl not in _BUILT:
        _BUILT[impl] = _build(impl, bin_dir)
    return _BUILT[impl]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def code(self) -> str | None:
        try:
            return self.json().get("code")
        except ValueError:
            return None


@dataclass
class Agent:
    impl: str
    exe: Path
    workdir: Path
    port: int
    proc: subprocess.Popen[bytes]
    log: Path
    env: dict[str, str] = field(default_factory=dict)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def connect(self, timeout: float = 60) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

    def request(self, method: str, path: str, body: bytes | None = None, *, token: str | None = TOKEN,
                headers: Mapping[str, str] | None = None, timeout: float = 60) -> Response:
        conn = self.connect(timeout)
        try:
            h = dict(headers or {})
            if token is not None:
                h["X-Sandbox-Token"] = token
            conn.request(method, path, body=body, headers=h)
            res = conn.getresponse()
            data = res.read()
            return Response(res.status, {k.lower(): v for k, v in res.getheaders()}, data)
        finally:
            conn.close()

    def exec(self, argv: list[str], *, stdin: bytes = b"", timeout_ms: int = 30000, env: Mapping[str, str] | None = None,
             exec_id: str | None = None, timeout: float = 90) -> Response:
        import base64

        req: dict[str, Any] = {"argv": argv, "stdin": base64.b64encode(stdin).decode(), "timeout_ms": timeout_ms}
        if env is not None:
            req["env"] = dict(env)
        if exec_id is not None:
            req["exec_id"] = exec_id
        return self.request("POST", "/v1/exec", json.dumps(req).encode(), timeout=timeout)

    def run(self, mode: str, *args: str, **kw: Any) -> Response:
        return self.exec([sys.executable, HELPER, mode, *args], **kw)

    def logs(self) -> str:
        try:
            return self.log.read_text("utf-8", "replace")
        except OSError:
            return ""


def b64(value: str | None) -> bytes:
    import base64

    return base64.b64decode(value or "")


def start_agent(exe: Path, workdir: Path, *, impl: str = "?", extra_env: Mapping[str, str] | None = None, user: int | None = None,
                wait: bool = True) -> Agent:
    port = free_port()
    log = Path(tempfile.mkstemp(prefix="agent-log-")[1])
    env = {
        "AGENT_TOKEN": TOKEN,
        "AGENT_ADDR": f"127.0.0.1:{port}",
        "AGENT_WORKDIR": str(workdir),
        "AGENT_OUTPUT_LIMIT": "1024",
        "AGENT_FILE_LIMIT": "4096",
        "AGENT_MAX_TIMEOUT_S": "60",
        "AGENT_ENV_PASSTHROUGH": "VISIBLE",
        "PATH": os.environ.get("PATH", ""),
        "VISIBLE": "yes",
        "SECRET_NOT_LISTED": "nope",
    }
    for k in ("SYSTEMROOT", "WINDIR", "PATHEXT"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    env.update(extra_env or {})
    env = {k: v for k, v in env.items() if v is not None}
    with open(log, "wb") as out:
        kwargs: dict[str, Any] = {}
        if user is not None:
            kwargs["user"] = user
            kwargs["group"] = user
            kwargs["extra_groups"] = []
        proc = subprocess.Popen([str(exe)], env=env, stdout=out, stderr=subprocess.STDOUT, cwd=str(workdir.parent), **kwargs)
    agent = Agent(impl, exe, workdir, port, proc, log, env)
    if wait:
        deadline = time.monotonic() + 20
        while True:
            if proc.poll() is not None:
                raise AssertionError(f"agent 退出了（{proc.returncode}）：{agent.logs()}")
            try:
                if agent.request("GET", "/healthz", token=None, timeout=2).status == 200:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError(f"agent 没有起来：{agent.logs()}")
            time.sleep(0.05)
    return agent


def stop_agent(agent: Agent) -> None:
    if agent.proc.poll() is None:
        agent.proc.kill()
    agent.proc.wait(timeout=10)
    agent.log.unlink(missing_ok=True)


@pytest.fixture(params=IMPLS)
def impl(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def exe(impl: str, bin_dir: Path) -> Path:
    return binary(impl, bin_dir)


@pytest.fixture
def make_agent(exe: Path, impl: str, tmp_path: Path) -> Iterator[Any]:
    started: list[Agent] = []
    counter = iter(range(1000))

    def make(extra_env: Mapping[str, str] | None = None, *, workdir: Path | None = None, **kw: Any) -> Agent:
        wd = workdir or (tmp_path / f"work{next(counter)}")
        a = start_agent(exe, wd, impl=impl, extra_env=extra_env, **kw)
        started.append(a)
        return a

    yield make
    for a in started:
        stop_agent(a)


@pytest.fixture
def agent(make_agent: Any) -> Agent:
    return make_agent()


@pytest.fixture
def both(bin_dir: Path, tmp_path: Path) -> Iterator[Any]:
    """两版各起一个（互通与对照用）；只测一版时跳过。"""
    if not {"go", "rust"} <= set(IMPLS):
        pytest.skip("要两版都测")
    started: list[Agent] = []
    counter = iter(range(1000))

    def make(which: str, extra_env: Mapping[str, str] | None = None) -> Agent:
        a = start_agent(binary(which, bin_dir), tmp_path / f"{which}{next(counter)}", impl=which, extra_env=extra_env)
        started.append(a)
        return a

    yield make
    for a in started:
        stop_agent(a)
