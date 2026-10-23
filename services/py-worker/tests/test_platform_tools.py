"""T-10 验收：平台内工具（细化见 docs/plan/tool-driver.md 第 2 节第 4 条、第 3 节）。

工具登记进开发用的 LocalToolDriverProvider，经 ToolDriver 的契约调用（参数校验、写操作要幂等键、幂等记录都由它负责）。覆盖：
- 登记：名字、版本、effect、说明与 input_schema；没配置工作区或主机名单时不登记对应的工具。
- repo_read、repo_list、repo_grep、repo_edit：按租户与 workspace 划分工作区；绝对路径、..、反斜杠、经符号链接出界都拒绝。
- shell_run：argv 列表、不经 shell；在工作区里执行；子进程拿不到 Worker 的环境变量；非零退出码照常返回；超时返回 timed_out。
- web_fetch：只去登记的主机；内网地址拒绝；不跟随重定向；大小上限；只认 http、https。
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.enums import Effect
from agentruntime_core.errors import ConflictError, DeniedError, InvalidError, NotFoundError
from agentruntime_core.providers.tool_driver import ToolDriver
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools

T1 = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1")
T1_OTHER_WORKSPACE = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w2")
T2 = TenantContext(tenant_id="t2", user_id="u2", workspace_id="w1")
SECRET_ENV = "AR_TEST_WORKER_SECRET"

WORKSPACE_TOOLS = {"repo_read", "repo_list", "repo_grep", "repo_edit", "shell_run"}
EFFECTS = {
    "repo_read": Effect.READ,
    "repo_list": Effect.READ,
    "repo_grep": Effect.READ,
    "repo_edit": Effect.WRITE_REVERSIBLE,
    "shell_run": Effect.WRITE_REVERSIBLE,
    "web_fetch": Effect.READ,
}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspaces" / "t1" / "w1"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "rounding.py").write_text("def round_half(x):\n    return int(x)\n\n# 旧实现\n", "utf-8", newline="\n")
    (workspace / "README.md").write_text("# demo\nrounding helpers\n", "utf-8", newline="\n")
    (tmp_path / "outside.txt").write_text("不该读到", "utf-8")
    return tmp_path


def make(root: Path, **overrides: Any) -> tuple[LocalToolDriverProvider, list[str]]:
    provider = LocalToolDriverProvider()
    config = PlatformToolsConfig(workspace_root=root / "workspaces", **overrides)
    return provider, register_platform_tools(provider, config)


def driver(provider: LocalToolDriverProvider, tenant: TenantContext = T1) -> ToolDriver:
    return provider.bind(tenant)


# --- 登记 ---


async def test_registered_tools_carry_effects_and_schemas(root: Path) -> None:
    provider, names = make(root, fetch_hosts=("*",))
    assert set(names) == WORKSPACE_TOOLS | {"web_fetch"}
    specs = {s.name: s for s in await driver(provider).list_tools()}
    for name, effect in EFFECTS.items():
        spec = specs[name]
        assert spec.version == "v1" and spec.effect == effect, f"{name} 的 effect 应为 {effect}"
        assert spec.description and spec.input_schema.get("type") == "object" and spec.input_schema.get("properties"), f"{name} 要有说明与参数 schema"
        assert not spec.permissions


def test_tools_are_registered_only_when_configured(tmp_path: Path) -> None:
    provider = LocalToolDriverProvider()
    assert register_platform_tools(provider, PlatformToolsConfig()) == [], "没配置工作区与主机名单时一个也不登记"
    only_fetch = register_platform_tools(LocalToolDriverProvider(), PlatformToolsConfig(fetch_hosts=("example.com",)))
    assert only_fetch == ["web_fetch"]
    only_workspace = register_platform_tools(LocalToolDriverProvider(), PlatformToolsConfig(workspace_root=tmp_path))
    assert set(only_workspace) == WORKSPACE_TOOLS


# --- 工作区 ---


async def test_repo_read_reads_a_window_of_lines(root: Path) -> None:
    provider, _ = make(root)
    whole = (await driver(provider).invoke("repo_read", "v1", {"path": "src/rounding.py"})).output
    assert whole["content"].startswith("def round_half") and whole["total_lines"] == 4 and not whole["truncated"]
    window = (await driver(provider).invoke("repo_read", "v1", {"path": "src/rounding.py", "start_line": 2, "max_lines": 1})).output
    assert window["content"] == "    return int(x)\n" and (window["start_line"], window["end_line"]) == (2, 2) and window["truncated"]


async def test_repo_list_and_grep(root: Path) -> None:
    provider, _ = make(root)
    listing = (await driver(provider).invoke("repo_list", "v1", {})).output
    assert [(e["name"], e["type"]) for e in listing["entries"]] == [("README.md", "file"), ("src", "dir")]
    found = (await driver(provider).invoke("repo_grep", "v1", {"pattern": r"round_?half|rounding"})).output
    assert {(m["path"], m["line"]) for m in found["matches"]} == {("README.md", 2), ("src/rounding.py", 1)}


async def test_workspaces_are_isolated_by_tenant_and_workspace(root: Path) -> None:
    provider, _ = make(root)
    for other in (T2, T1_OTHER_WORKSPACE):
        with pytest.raises(NotFoundError):
            await driver(provider, other).invoke("repo_read", "v1", {"path": "src/rounding.py"})


@pytest.mark.parametrize("path", ["../outside.txt", "../../outside.txt", "/etc/passwd", "C:/Windows/win.ini", "src\\rounding.py", "src/../../outside.txt"])
async def test_paths_cannot_leave_the_workspace(root: Path, path: str) -> None:
    provider, _ = make(root)
    with pytest.raises(InvalidError):
        await driver(provider).invoke("repo_read", "v1", {"path": path})


async def test_symlinks_cannot_leave_the_workspace(root: Path) -> None:
    link = root / "workspaces" / "t1" / "w1" / "escape.txt"
    try:
        link.symlink_to(root / "outside.txt")
    except OSError:
        pytest.skip("这台机器不允许建符号链接")
    provider, _ = make(root)
    with pytest.raises(InvalidError):
        await driver(provider).invoke("repo_read", "v1", {"path": "escape.txt"})


async def test_repo_edit_replaces_a_unique_occurrence(root: Path) -> None:
    provider, _ = make(root)
    tools = driver(provider)
    result = await tools.invoke("repo_edit", "v1", {"path": "src/rounding.py", "old": "return int(x)", "new": "return int(x + 0.5)"}, idempotency_key="idk_edit_1")
    assert result.output["path"] == "src/rounding.py" and not result.output["created"]
    assert "int(x + 0.5)" in (root / "workspaces" / "t1" / "w1" / "src" / "rounding.py").read_text("utf-8")
    with pytest.raises(InvalidError):  # 已经改过，找不到原文
        await tools.invoke("repo_edit", "v1", {"path": "src/rounding.py", "old": "return int(x)", "new": "?"}, idempotency_key="idk_edit_2")
    (root / "workspaces" / "t1" / "w1" / "twice.txt").write_text("a a", "utf-8")
    with pytest.raises(InvalidError):  # 不唯一
        await tools.invoke("repo_edit", "v1", {"path": "twice.txt", "old": "a", "new": "b"}, idempotency_key="idk_edit_3")
    created = await tools.invoke("repo_edit", "v1", {"path": "notes/new.md", "new": "# 新文件\n"}, idempotency_key="idk_edit_4")
    assert created.output["created"] and (root / "workspaces" / "t1" / "w1" / "notes" / "new.md").read_text("utf-8") == "# 新文件\n"


async def test_repo_edit_is_a_write_with_idempotency(root: Path) -> None:
    provider, _ = make(root)
    tools = driver(provider)
    args = {"path": "README.md", "old": "rounding helpers", "new": "rounding helpers (fixed)"}
    with pytest.raises(InvalidError):
        await tools.invoke("repo_edit", "v1", args)  # 写操作必须带幂等键
    first = await tools.invoke("repo_edit", "v1", args, idempotency_key="idk_readme")
    again = await tools.invoke("repo_edit", "v1", args, idempotency_key="idk_readme")
    assert not first.replayed and again.replayed, "同一幂等键只执行一次"
    assert (root / "workspaces" / "t1" / "w1" / "README.md").read_text("utf-8").count("(fixed)") == 1
    with pytest.raises(ConflictError):
        await tools.invoke("repo_edit", "v1", {**args, "new": "别的"}, idempotency_key="idk_readme")


# --- shell_run ---


async def test_shell_run_runs_in_the_workspace_without_the_workers_environment(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "worker-only-secret-9c1d")
    provider, _ = make(root)
    code = "import os, sys; print(sorted(os.listdir('.'))); print(os.environ.get('%s', 'absent')); sys.exit(3)" % SECRET_ENV
    out = (await driver(provider).invoke("shell_run", "v1", {"argv": [sys.executable, "-c", code]}, idempotency_key="idk_shell_1")).output
    assert out["exit_code"] == 3 and not out["timed_out"], "非零退出码照常返回，不当作工具失败"
    assert "'README.md', 'src'" in out["stdout"], "在本租户的工作区里执行"
    assert "absent" in out["stdout"] and "worker-only-secret" not in out["stdout"], "子进程拿不到 Worker 的环境变量"


async def test_shell_run_times_out(root: Path) -> None:
    provider, _ = make(root)
    args = {"argv": [sys.executable, "-c", "import time; time.sleep(30)"], "timeout_s": 1}
    out = (await driver(provider).invoke("shell_run", "v1", args, idempotency_key="idk_shell_2")).output
    assert out["timed_out"] and out["exit_code"] is None


async def test_shell_run_takes_an_argv_list_not_a_shell_string(root: Path) -> None:
    provider, _ = make(root)
    with pytest.raises(InvalidError):
        await driver(provider).invoke("shell_run", "v1", {"argv": "echo hi && rm -rf /"}, idempotency_key="idk_shell_3")


# --- web_fetch ---


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        body = ("<html><body>存储价格：每 GB 0.02 美元</body></html>" if self.path != "/big" else "x" * 200_000).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture
def site() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


async def test_web_fetch_reads_an_allowed_page(root: Path, site: str) -> None:
    provider, _ = make(root, fetch_hosts=("127.0.0.1",), allow_private_fetch=True)
    out = (await driver(provider).invoke("web_fetch", "v1", {"url": site + "/pricing"})).output
    assert out["status"] == 200 and "0.02" in out["text"] and out["content_type"].startswith("text/html")


async def test_web_fetch_does_not_follow_redirects(root: Path, site: str) -> None:
    provider, _ = make(root, fetch_hosts=("127.0.0.1",), allow_private_fetch=True)
    out = (await driver(provider).invoke("web_fetch", "v1", {"url": site + "/redirect"})).output
    assert out["status"] == 302 and out["location"].startswith("http://169.254.169.254"), "重定向不跟随，要去就再调一次（再检查一次）"


async def test_web_fetch_caps_the_size(root: Path, site: str) -> None:
    provider, _ = make(root, fetch_hosts=("127.0.0.1",), allow_private_fetch=True)
    out = (await driver(provider).invoke("web_fetch", "v1", {"url": site + "/big", "max_bytes": 1000})).output
    assert out["truncated"] and len(out["text"]) <= 1000


async def test_web_fetch_only_goes_to_listed_hosts(root: Path, site: str) -> None:
    provider, _ = make(root, fetch_hosts=("example.com",), allow_private_fetch=True)
    with pytest.raises(DeniedError):
        await driver(provider).invoke("web_fetch", "v1", {"url": site + "/pricing"})


async def test_web_fetch_refuses_private_addresses(root: Path, site: str) -> None:
    provider, _ = make(root, fetch_hosts=("*",))
    for url in (site + "/pricing", "http://169.254.169.254/latest/meta-data/", "http://[::1]/"):
        with pytest.raises(DeniedError):
            await driver(provider).invoke("web_fetch", "v1", {"url": url})


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "not a url"])
async def test_web_fetch_only_speaks_http(root: Path, url: str) -> None:
    provider, _ = make(root, fetch_hosts=("*",))
    with pytest.raises(InvalidError):
        await driver(provider).invoke("web_fetch", "v1", {"url": url})
