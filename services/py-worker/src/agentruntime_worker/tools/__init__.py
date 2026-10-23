"""ToolDriver 的 Worker 一侧（设计书 4.9；T-10，细化见 docs/plan/tool-driver.md）。

- dev_tools(config)：Worker 进程内唯一的开发用 ToolDriver（LocalToolDriverProvider）。按配置登记平台内工具（platform.py）；
  Node Executor、resolve_catalog、tool_loop_step 共用它。同样的工具配置得到同一个实例。
- tool_sessions(config)：Worker 运行期间的 MCP 会话：配了 AR_MCP_TOOLS_FILE 时启动 MCP Gateway（mcp.py），把登记了的
  MCP 工具登记进 dev_tools(config)，退出时关闭会话。
- ToolCatalog、DevCatalog、latest_spec：工具的 effect 与定义取自登记（catalog.py）。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from agentruntime_core.dev.tool_driver import LocalToolDriverProvider

from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.tools.catalog import DevCatalog, ToolCatalog, latest_spec
from agentruntime_worker.tools.mcp import McpGateway
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools

__all__ = ["DevCatalog", "ToolCatalog", "dev_tools", "latest_spec", "tool_sessions"]

_providers: dict[tuple[object, ...], LocalToolDriverProvider] = {}


def _key(config: WorkerConfig) -> tuple[object, ...]:
    return (config.tool_workspace_root, config.tool_fetch_hosts, config.mcp_tools_file)


def dev_tools(config: WorkerConfig) -> LocalToolDriverProvider:
    """开发用 ToolDriver：没配置时是空的（一个工具也没有）。"""
    key = _key(config)
    provider = _providers.get(key)
    if provider is None:
        provider = LocalToolDriverProvider()
        register_platform_tools(provider, PlatformToolsConfig(workspace_root=config.tool_workspace_root, fetch_hosts=config.tool_fetch_hosts))
        _providers[key] = provider
    return provider


@contextlib.asynccontextmanager
async def tool_sessions(config: WorkerConfig) -> AsyncIterator[McpGateway | None]:
    """Worker 运行期间的 MCP 会话；没配 AR_MCP_TOOLS_FILE 时什么也不做（给出 None）。"""
    if config.mcp_tools_file is None:
        yield None
        return
    async with McpGateway(dev_tools(config), config.mcp_tools_file) as gateway:
        yield gateway
