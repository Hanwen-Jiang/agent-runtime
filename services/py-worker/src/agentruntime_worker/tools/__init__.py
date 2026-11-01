"""ToolDriver 的 Worker 一侧（设计书 4.9；T-10，细化见 docs/plan/tool-driver.md）。

- dev_tools(config)：Worker 进程内唯一的开发用 ToolDriver（LocalToolDriverProvider）。按配置登记平台内工具（platform.py）；
  Node Executor、resolve_catalog、tool_loop_step 共用它。同样的工具配置得到同一个实例。context_read 总是登记。
- sandbox_broker(config)：shell_run 经它借还沙箱环境（sandbox.broker）：登记的后端、按租户策略选，建与删都记进沙箱环境表
  （业务库：AR_RELATIONAL_DSN 的 Postgres，没设时数据目录下的 SQLite；D-11）；对账的 activity 用同一个。存档模式的
  工作区（D-06）经它的 WorkspaceArchiver：快照记在同一个业务库，块与清单放在 context_objects 的 ObjectStore。
- context_objects(config)：Context Builder 外置内容的 ObjectStore，context_read 读的也是它（R-03 评审 D11）。位置同
  Node Executor（设了 AR_S3_ENDPOINT 时是 S3 兼容的对象存储，否则 AR_OBJECT_STORE_DIR 或数据目录下的 objects）；
  都没配时是进程内同一个临时目录。
- tool_sessions(config)：Worker 运行期间的 MCP 会话：配了 AR_MCP_TOOLS_FILE 时启动 MCP Gateway（mcp.py），把登记了的
  MCP 工具登记进 dev_tools(config)，退出时关闭会话。mcp_tool_names(config) 是现在登记了的 MCP 工具的名字（上下文占比里单独一类）。
- ToolCatalog、DevCatalog、latest_spec：工具的 effect 与定义取自登记（catalog.py）。
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from datetime import timedelta
from collections.abc import AsyncIterator
from pathlib import Path

from agentruntime_core import envcred
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.providers.object_store import ObjectStoreProvider

from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.sandbox.broker import SandboxBroker
from agentruntime_worker.sandbox.credentials import TEST_ENV_SECRET, EnvCredentials
from agentruntime_worker.sandbox.ledger import SandboxLedger
from agentruntime_worker.sandbox.store import relational_store
from agentruntime_worker.telemetry import worker_telemetry
from agentruntime_worker.tools.catalog import DevCatalog, ToolCatalog, latest, latest_spec
from agentruntime_worker.tools.mcp import McpGateway
from agentruntime_worker.tools.platform import CONTEXT_READ, PlatformToolsConfig, register_platform_tools
from agentruntime_worker.tools.sandboxes import Sandboxes, registry_policy
from agentruntime_worker.workspace.archive import WorkspaceArchiver
from agentruntime_worker.workspace.operations import CREDENTIAL_SECRET, WorkspaceOperations

__all__ = [
    "CONTEXT_READ", "DevCatalog", "ToolCatalog", "context_objects", "dev_tools", "latest", "latest_spec", "mcp_tool_names", "sandbox_broker",
    "tool_sessions", "workspace_operations",
]

_providers: dict[tuple[object, ...], LocalToolDriverProvider] = {}
_objects: dict[tuple[object, ...], ObjectStoreProvider] = {}
_brokers: dict[tuple[object, ...], SandboxBroker | None] = {}
_mcp_names: dict[tuple[object, ...], frozenset[str]] = {}


def _store_key(config: WorkerConfig) -> tuple[object, ...]:
    return (config.data_dir, config.object_store_dir, config.object_store_s3)


def _key(config: WorkerConfig) -> tuple[object, ...]:
    return (
        config.tool_workspace_root, config.tool_fetch_hosts, config.mcp_tools_file, config.tool_shell_sandbox,
        config.sandbox_backends_file, config.registry_address, config.sandbox_sessions, config.sandbox_runtime, config.preview_domain,
        *_broker_key(config), *_store_key(config),
    )


def _broker_key(config: WorkerConfig) -> tuple[object, ...]:
    return (
        config.tool_shell_sandbox, config.sandbox_backends_file, config.registry_address, config.data_dir, config.secrets_file, config.relational_dsn,
        config.pkg_mirror_url, config.egress_gateway_url, *_store_key(config),
    )


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
            credentials = EnvCredentials(
                lambda: boxes.read_secret(envcred.SECRET_NAME), mirror_url=config.pkg_mirror_url, egress_url=config.egress_gateway_url,
                test_env=lambda tenant: boxes.read_tenant_secret(tenant, TEST_ENV_SECRET),
            )
            store = relational_store(config.relational_dsn, root)
            # 存档模式的工作区（D-06）：快照的行在同一个业务库，块与清单在 Context Builder 用的同一个 ObjectStore（租户作用域）。
            archiver = WorkspaceArchiver(store, context_objects(config))
            broker = SandboxBroker(boxes, SandboxLedger(store), credentials=credentials, archiver=archiver)
            broker.hold_max = timedelta(seconds=config.sandbox_hold_max_s)
            if boxes.environments:
                # 按项目定制的运行环境（D-17；登记文件的 environments 一节，格式见 sandbox.env_build.environment_manager）。
                from agentruntime_worker.sandbox.env_build import environment_manager

                broker.environments = environment_manager(broker, boxes.environments)
            _brokers[key] = broker
    return _brokers[key]


def workspace_operations(config: WorkerConfig) -> WorkspaceOperations | None:
    """Workspace API 的操作（D-06e）：经 sandbox_broker 在承载工作区的沙箱里执行。shell_run 不经沙箱（host、docker）但配了
    工作区目录时，用一个只登记本地后端（挂工作区目录）的 broker：操作直接在 <工作区根>/<租户>/<workspace> 里跑 git，与
    shell_run、repo_* 是同一处。两者都没有时为 None（activity 报 workspace_api_unavailable）。封凭据的密钥取 SecretStore
    平台作用域的 workspace/credential-key。"""
    broker = sandbox_broker(config) or host_broker(config)
    if broker is None:
        return None
    return WorkspaceOperations(
        broker, secret=lambda: broker.sandboxes.read_secret(CREDENTIAL_SECRET), runtime=config.sandbox_runtime,
        allow_local_remotes=config.workspace_allow_local_remotes,
    )


def host_broker(config: WorkerConfig) -> SandboxBroker | None:
    """没配沙箱时 Workspace API 用的 broker：只登记一个挂工作区目录的本地后端（不隔离，同 shell_run 的 host）；环境表、
    快照与工作区的行记在同一个业务库。没配工作区目录时为 None。"""
    if config.tool_workspace_root is None:
        return None
    key = ("host", *_broker_key(config), config.tool_workspace_root)
    if key not in _brokers:
        boxes = Sandboxes(
            {"host": {"kind": "local", "isolation": "none"}}, "host", workspace_root=config.tool_workspace_root, secrets_file=config.secrets_file,
            policy=registry_policy(config.registry_address) if config.registry_address else None,
        )
        root = config.data_dir or Path(tempfile.mkdtemp(prefix="agentruntime-sandbox-ledger-"))
        store = relational_store(config.relational_dsn, root)
        _brokers[key] = SandboxBroker(boxes, SandboxLedger(store), archiver=WorkspaceArchiver(store, context_objects(config)))
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
                network_requests=bool(config.egress_gateway_url),
                preview_domain=config.preview_domain,
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
        _mcp_names[_key(config)] = frozenset(gateway.registered)
        try:
            yield gateway
        finally:
            _mcp_names.pop(_key(config), None)


def mcp_tool_names(config: WorkerConfig) -> frozenset[str]:
    return _mcp_names.get(_key(config), frozenset())
