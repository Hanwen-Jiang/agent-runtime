"""T-10 验收：MCP Gateway（设计书 4.9、附录 L.10；细化见 docs/plan/tool-driver.md 第 2 节第 5 条）。

假的 MCP Server（mcp_fake_server.py）经 stdio 跑在子进程里。覆盖：
- 登记了的 MCP 工具补齐 effect、版本、超时，说明与 input_schema 取自 Server，登记进 ToolDriver；没登记的不能调用；
  登记了但 Server 没有的跳过；Server 起不来时它的工具都跳过，别的照常。
- 调用经 ToolDriver 的同一套规则：参数校验在到达 Server 之前；写操作要幂等键、同一键只执行一次；超时是 transient；
  isError 是 invalid；会话崩溃这次是 transient、下次重启。
- 租户范围；登记文件不合格就报错；退出时关闭会话。
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.enums import Effect
from agentruntime_core.errors import InvalidError, NotFoundError, TransientError
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.tools.mcp import McpGateway, McpRegistrationError, load_registration

SERVER = Path(__file__).with_name("mcp_fake_server.py")
T1 = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1")
T2 = TenantContext(tenant_id="t2", user_id="u2", workspace_id="w1")


def registration(log: Path, *, command: list[str] | None = None, extra_servers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "servers": [
            {
                "name": "fake",
                "command": command or [sys.executable, str(SERVER)],
                "env": {"FAKE_MCP_LOG": str(log)},
                "tools": [
                    {"tool": "echo", "name": "mcp_echo", "version": "v1", "effect": "read", "timeout_s": 5},
                    {"tool": "shout", "name": "mcp_shout", "effect": "none"},
                    {"tool": "fail", "name": "mcp_fail", "effect": "none"},
                    {"tool": "slow", "name": "mcp_slow", "effect": "read", "timeout_s": 1},
                    {"tool": "crash", "name": "mcp_crash", "effect": "read"},
                    {"tool": "write_note", "name": "mcp_write_note", "effect": "write_reversible"},
                    {"tool": "missing_on_server", "name": "mcp_missing", "effect": "read"},
                    {"tool": "echo", "name": "mcp_echo_t1", "effect": "read", "tenants": ["t1"]},
                ],
            },
            *(extra_servers or []),
        ]
    }


def calls(log: Path) -> list[str]:
    return log.read_text("utf-8").splitlines() if log.exists() else []


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "calls.log"


async def test_registered_tools_are_wrapped_with_the_declared_metadata(log: Path) -> None:
    provider = LocalToolDriverProvider()
    async with McpGateway(provider, registration(log)) as gateway:
        specs = {s.name: s for s in await provider.bind(T1).list_tools()}
        echo = specs["mcp_echo"]
        assert (echo.version, echo.effect, echo.timeout) == ("v1", Effect.READ, timedelta(seconds=5))
        assert echo.description == "回显 text", "说明取自 Server"
        assert echo.input_schema["required"] == ["text"], "input_schema 取自 Server"
        assert specs["mcp_write_note"].effect == Effect.WRITE_REVERSIBLE and not specs["mcp_write_note"].permissions
        assert "secret_tool" not in specs and "mcp_missing" not in specs
        assert "mcp_missing" in gateway.skipped, "登记了但 Server 没有的跳过"
        assert set(gateway.registered) >= {"mcp_echo", "mcp_shout", "mcp_write_note"}


async def test_unregistered_tools_cannot_be_called(log: Path) -> None:
    provider = LocalToolDriverProvider()
    async with McpGateway(provider, registration(log)):
        for name in ("secret_tool", "mcp_missing"):
            with pytest.raises(NotFoundError):
                await provider.bind(T1).invoke(name, "v1", {})
    assert "secret_tool" not in calls(log)


async def test_calls_go_through_the_session(log: Path) -> None:
    provider = LocalToolDriverProvider()
    async with McpGateway(provider, registration(log)):
        tools = provider.bind(T1)
        assert (await tools.invoke("mcp_echo", "v1", {"text": "你好"})).output == {"text": "你好"}, "有结构化输出就用它"
        assert (await tools.invoke("mcp_shout", "v1", {"text": "hi"})).output == {"text": "HI"}, "只有文本时包成 {text}"
    assert calls(log) == ["echo", "shout"]


async def test_tool_errors_are_invalid(log: Path) -> None:
    provider = LocalToolDriverProvider()
    async with McpGateway(provider, registration(log)):
        with pytest.raises(InvalidError) as caught:
            await provider.bind(T1).invoke("mcp_fail", "v1", {})
    assert caught.value.code == "tool_error"


async def test_arguments_are_checked_before_reaching_the_server(log: Path) -> None:
    provider = LocalToolDriverProvider()
    async with McpGateway(provider, registration(log)):
        with pytest.raises(InvalidError):
            await provider.bind(T1).invoke("mcp_echo", "v1", {"wrong": 1})
    assert calls(log) == []


async def test_writes_need_keys_and_execute_once(log: Path) -> None:
    provider = LocalToolDriverProvider()
    async with McpGateway(provider, registration(log)):
        tools = provider.bind(T1)
        with pytest.raises(InvalidError):
            await tools.invoke("mcp_write_note", "v1", {"note": "a"})
        first = await tools.invoke("mcp_write_note", "v1", {"note": "a"}, idempotency_key="idk_note")
        again = await tools.invoke("mcp_write_note", "v1", {"note": "a"}, idempotency_key="idk_note")
    assert first.output == {"saved": True} and again.replayed
    assert calls(log).count("write_note") == 1, "同一幂等键只真正执行一次"


async def test_timeouts_are_transient(log: Path) -> None:
    provider = LocalToolDriverProvider()
    async with McpGateway(provider, registration(log)):
        with pytest.raises(TransientError):
            await provider.bind(T1).invoke("mcp_slow", "v1", {"seconds": 5})


async def test_a_crashed_session_is_restarted(log: Path) -> None:
    provider = LocalToolDriverProvider()
    async with McpGateway(provider, registration(log)):
        tools = provider.bind(T1)
        with pytest.raises(TransientError):
            await tools.invoke("mcp_crash", "v1", {})
        assert (await tools.invoke("mcp_echo", "v1", {"text": "还在"})).output == {"text": "还在"}, "下次调用重启会话"


async def test_tenant_scoped_tools(log: Path) -> None:
    provider = LocalToolDriverProvider()
    async with McpGateway(provider, registration(log)):
        assert "mcp_echo_t1" in {s.name for s in await provider.bind(T1).list_tools()}
        assert "mcp_echo_t1" not in {s.name for s in await provider.bind(T2).list_tools()}
        with pytest.raises(NotFoundError):
            await provider.bind(T2).invoke("mcp_echo_t1", "v1", {"text": "x"})


async def test_a_server_that_cannot_start_registers_nothing(log: Path, tmp_path: Path) -> None:
    broken = {"name": "broken", "command": [str(tmp_path / "no-such-server")], "tools": [{"tool": "x", "name": "mcp_broken", "effect": "read"}]}
    provider = LocalToolDriverProvider()
    async with McpGateway(provider, registration(log, extra_servers=[broken])) as gateway:
        names = {s.name for s in await provider.bind(T1).list_tools()}
        assert "mcp_broken" not in names and "mcp_broken" in gateway.skipped
        assert "mcp_echo" in names, "别的 Server 照常"


async def test_sessions_are_closed_on_exit(log: Path) -> None:
    provider = LocalToolDriverProvider()
    gateway = McpGateway(provider, registration(log))
    async with gateway:
        await provider.bind(T1).invoke("mcp_echo", "v1", {"text": "x"})
        assert any(s.alive for s in gateway.sessions)
    assert not any(s.alive for s in gateway.sessions), "退出时关闭会话（子进程结束）"


def tool_entry(**overrides: Any) -> dict[str, Any]:
    return {"tool": "echo", "name": "mcp_echo", "effect": "read", **overrides}


@pytest.mark.parametrize(
    "tool",
    [
        {"tool": "echo", "name": "mcp_echo"},  # 缺 effect：MCP 没有这个字段，必须人工登记
        tool_entry(effect="write"),
        tool_entry(permissions=["crm.read"]),  # 第一期不支持权限
        tool_entry(name="MCP Echo"),
        tool_entry(version="1"),
        tool_entry(timeout_s=0),
    ],
)
def test_bad_registrations_are_rejected(tool: dict[str, Any]) -> None:
    with pytest.raises(McpRegistrationError) as caught:
        load_registration({"servers": [{"name": "fake", "command": ["python"], "tools": [tool]}]})
    assert isinstance(caught.value, InvalidError) and isinstance(caught.value, ValueError)


def test_duplicate_names_are_rejected() -> None:
    with pytest.raises(McpRegistrationError):
        load_registration({"servers": [{"name": "fake", "command": ["python"], "tools": [tool_entry(), tool_entry(tool="shout")]}]})


def test_a_registration_file_is_read_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "mcp-tools.json"
    path.write_text('{"servers": [{"name": "fake", "command": ["python"], "tools": [{"tool": "echo", "name": "mcp_echo", "effect": "read"}]}]}', "utf-8")
    servers = load_registration(path)
    (server,) = servers
    (tool,) = server.tools
    assert (server.name, tool.name, tool.version, tool.effect) == ("fake", "mcp_echo", "v1", Effect.READ), "版本默认 v1"
