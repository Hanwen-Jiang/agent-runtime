"""ToolDriver 的 Worker 一侧（设计书 4.9；T-10，细化见 docs/plan/tool-driver.md）。

- dev_tools(config)：Worker 进程内唯一的开发用 ToolDriver（LocalToolDriverProvider）。按配置登记平台内工具（platform.py）；
  Node Executor、resolve_catalog、tool_loop_step 共用它。同样的工具配置得到同一个实例。context_read 总是登记。
- sandbox_broker(config)：shell_run 经它借还沙箱环境（sandbox.broker）：登记的后端、按租户策略选，建与删都记进沙箱环境表
  （业务库：AR_RELATIONAL_DSN 的 Postgres，没设时数据目录下的 SQLite；D-11）；对账的 activity 用同一个。
- context_objects(config)：Context Builder 外置内容的 ObjectStore，context_read 读的也是它（R-03 评审 D11）。位置同
  Node Executor（设了 AR_S3_ENDPOINT 时是 S3 兼容的对象存储，否则 AR_OBJECT_STORE_DIR 或数据目录下的 objects）；
  都没配时是进程内同一个临时目录。
- tool_sessions(config)：Worker 运行期间的 MCP 会话：配了 AR_MCP_TOOLS_FILE 时启动 MCP Gateway（mcp.py），把登记了的
  MCP 工具登记进 dev_tools(config)，退出时关闭会话。
- ToolCatalog、DevCatalog、latest_spec：工具的 effect 与定义取自登记（catalog.py）。
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.providers.object_store import ObjectStoreProvider

from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.sandbox.broker import SandboxBroker
from agentruntime_worker.sandbox.ledger import SandboxLedger
from agentruntime_worker.sandbox.store import relational_store
from agentruntime_worker.telemetry import worker_telemetry
from agentruntime_worker.tools.catalog import DevCatalog, ToolCatalog, latest, latest_spec
from agentruntime_worker.tools.mcp import McpGateway
from agentruntime_worker.tools.platform import CONTEXT_READ, PlatformToolsConfig, register_platform_tools
from agentruntime_worker.tools.sandboxes import Sandboxes, registry_policy

__all__ = [
    "CONTEXT_READ", "DevCatalog", "ToolCatalog", "context_objects", "dev_tools", "latest", "latest_spec", "sandbox_broker", "tool_sessions",
]

_providers: dict[tuple[object, ...], LocalToolDriverProvider] = {}
_objects: dict[tuple[object, ...], ObjectStoreProvider] = {}
_brokers: dict[tuple[object, ...], SandboxBroker | None] = {}


def _store_key(config: WorkerConfig) -> tuple[object, ...]:
    return (config.data_dir, config.object_store_dir, config.object_store_s3)


def _key(config: WorkerConfig) -> tuple[object, ...]:
    return (
        config.tool_workspace_root, config.tool_fetch_hosts, config.mcp_tools_file, config.tool_shell_sandbox,
        config.sandbox_backends_file, config.registry_address, config.sandbox_sessions, config.sandbox_runtime, *_store_key(config),
    )


def _broker_key(config: WorkerConfig) -> tuple[object, ...]:
    return (config.tool_shell_sandbox, config.sandbox_backends_file, config.registry_address, config.data_dir, config.secrets_file, config.relational_dsn)


def sandboxes(config: WorkerConfig) -> Sandboxes | None:
    """shell_run 的沙箱后端（AR_TOOL_SHELL_SANDBOX=sandbox 时）：登记文件里的后端，按租户策略（经 Registry）选。"""
    if config.tool_shell_sandbox != "sandbox" or config.sandbox_backends_file is None:
        return None
    return Sandboxes.from_file(
        config.sandbox_backends_file,
        workspace_root=config.tool_workspace_root,
        secrets_file=config.secrets_file,
        policy=registry_policy(config.registry_address) if config.registry_address else None,
    )


def sandbox_broker(config: WorkerConfig) -> SandboxBroker | None:
    """shell_run 与对账共用的 SandboxBroker（同样的配置得到同一个实例）；不是 sandbox 时为 None。"""
    key = _broker_key(config)
    if key not in _brokers:
        boxes = sandboxes(config)
        if boxes is None:
            _brokers[key] = None
        else:
            root = config.data_dir or Path(tempfile.mkdtemp(prefix="agentruntime-sandbox-ledger-"))
            _brokers[key] = SandboxBroker(boxes, SandboxLedger(relational_store(config.relational_dsn, root)))
    return _brokers[key]


def context_objects(config: WorkerConfig) -> ObjectStoreProvider:
    """Context Builder 外置内容与 context_read 共用的 ObjectStore；同样的存储配置得到同一个实例。"""
    key = _store_key(config)
    objects = _objects.get(key)
    if objects is None:
        if config.object_store_s3 is not None:
            objects = config.object_store_s3.provider()
        else:
            root = config.data_dir or Path(tempfile.mkdtemp(prefix="agentruntime-context-"))
            path, _ = config.store_paths(root)
            objects = LocalDiskObjectStoreProvider(path, signing_key=os.urandom(32))
        _objects[key] = objects
    return objects


def dev_tools(config: WorkerConfig) -> LocalToolDriverProvider:
    """开发用 ToolDriver：没配置时只有 context_read。"""
    key = _key(config)
    provider = _providers.get(key)
    if provider is None:
        provider = LocalToolDriverProvider()
        register_platform_tools(
            provider,
            PlatformToolsConfig(
                workspace_root=config.tool_workspace_root,
                fetch_hosts=config.tool_fetch_hosts,
                shell_sandbox=config.tool_shell_sandbox,
                shell_image=config.tool_shell_image,
                session_sandboxes=config.sandbox_sessions,
                shell_runtime=config.sandbox_runtime,
            ),
            objects=context_objects(config),
            sandboxes=sandbox_broker(config),
            telemetry=worker_telemetry(),
        )
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
