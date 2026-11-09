"""ToolDriver 的 Worker 一侧（设计书 4.9；T-10，细化见 docs/plan/tool-driver.md）。

- dev_tools(config)：Worker 进程内唯一的开发用 ToolDriver（LocalToolDriverProvider）。按配置登记平台内工具（platform.py）；
  Node Executor、resolve_catalog、tool_loop_step 共用它。同样的工具配置得到同一个实例。context_read 总是登记。
- sandbox_broker(config)：Sandbox Service 的客户端（SBX-01，sandbox.client.ServiceBroker；设了 AR_SANDBOX_SERVICE_ADDRESS 时）。
  shell_run、repo_*、Workspace 操作与 sandbox_exec 都经它借还环境；选后端、配额、复用、存档与恢复、对账都在服务里。它带着
  Worker 这边的 WorkspaceArchiver（工作区记录、版本号、推送记录；业务库同 task-service）。
- context_objects(config)：Context Builder 外置内容的 ObjectStore，context_read 读的也是它（R-03 评审 D11）。位置同
  Node Executor（设了 AR_S3_ENDPOINT 时是 S3 兼容的对象存储，否则 AR_OBJECT_STORE_DIR 或数据目录下的 objects）；
  都没配时是进程内同一个临时目录。
- tool_sessions(config)：Worker 运行期间的 MCP 会话：配了 AR_MCP_TOOLS_FILE 时启动 MCP Gateway（mcp.py），把登记了的
  MCP 工具登记进 dev_tools(config)，退出时关闭会话。mcp_tool_names(config) 是现在登记了的 MCP 工具的名字（上下文占比里单独一类）。
- mcp_leases(config)：用户登记的远程 MCP 工具（N-15，mcp_http.py）：按 Task 的租约（业务库 mcp_leases，task-service 写）给出目录与
  调用，Streamable HTTP 会话按（租户，连接）在进程里持有；网络密钥从 AR_SECRETS_FILE 的租户作用域现读。AR_MCP_DENY_CIDRS 另加拒绝的网段。
- ToolCatalog、DevCatalog、latest_spec：工具的 effect 与定义取自登记（catalog.py）。
- deliver_file（deliver.py，N-18）：配了工作区目录时登记，把工作区里的文件作为 Task 的交付。
- skill_read（skills.py，N-14）：配了 Registry（AR_REGISTRY_ADDRESS）时登记，按 Task 固定的版本读 Skill；固定的版本取自业务库的
  tasks.skill_pins（skill_pins(config)，Planner 的目录读同一处）。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import json

from agentruntime_core import envcred
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.providers.object_store import ObjectStoreProvider

from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.sandbox.client import ServiceBroker, secrets_token
from agentruntime_worker.sandbox.grants import GrantStore
from agentruntime_worker.sandbox.policy import TenantPolicies, registry_policy
from agentruntime_worker.sandbox.store import relational_store
from agentruntime_worker.telemetry import worker_telemetry
from agentruntime_worker.tools.catalog import DevCatalog, ToolCatalog, latest, latest_spec
from agentruntime_worker.tools.deliver import DeliveryLimits, register_delivery_tools
from agentruntime_worker.tools.mcp import McpGateway
from agentruntime_worker.tools.mcp_http import LeasedMcpTools, LeaseStore
from agentruntime_worker.tools.platform import CONTEXT_READ, PlatformToolsConfig, register_platform_tools, workspace_files
from agentruntime_worker.tools.skills import TaskSkillPins, register_skill_tools
from agentruntime_worker.workspace.archive import WorkspaceArchiver
from agentruntime_worker.workspace.operations import CREDENTIAL_SECRET, WorkspaceOperations

__all__ = [
    "CONTEXT_READ", "DevCatalog", "ToolCatalog", "context_objects", "dev_tools", "latest", "latest_spec", "mcp_leases", "mcp_tool_names", "sandbox_broker",
    "skill_pins", "tool_sessions", "workspace_operations",
]

_providers: dict[tuple[object, ...], LocalToolDriverProvider] = {}
_objects: dict[tuple[object, ...], ObjectStoreProvider] = {}
_brokers: dict[tuple[object, ...], ServiceBroker | None] = {}
_mcp_names: dict[tuple[object, ...], frozenset[str]] = {}
_pins: dict[tuple[object, ...], TaskSkillPins] = {}
_leases: dict[tuple[object, ...], LeasedMcpTools] = {}


def _deny_cidrs() -> tuple[object, ...]:
    import ipaddress

    out = []
    for part in os.environ.get("AR_MCP_DENY_CIDRS", "").split(","):
        if part.strip():
            out.append(ipaddress.ip_network(part.strip(), strict=False))
    return tuple(out)


def mcp_leases(config: WorkerConfig) -> LeasedMcpTools:
    """Task 租约里的远程 MCP 工具（N-15）；同样的配置得到同一个实例（会话在里面）。"""
    key = (config.relational_dsn, config.data_dir, config.secrets_file)
    if key not in _leases:
        store = relational_store(config.relational_dsn, config.data_dir or Path(tempfile.mkdtemp(prefix="agentruntime-mcp-")))

        async def read_secret(tenant_id: str, name: str) -> str | None:
            if config.secrets_file is None:
                return None
            from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
            from agentruntime_core.tenancy import TenantContext

            value = (await LocalFileSecretStoreProvider(config.secrets_file).bind(TenantContext(tenant_id=tenant_id)).read(name)).reveal()
            return value.decode() if isinstance(value, bytes) else value

        _leases[key] = LeasedMcpTools(LeaseStore(store), read_secret, deny=_deny_cidrs())
    return _leases[key]


def skill_pins(config: WorkerConfig) -> TaskSkillPins:
    """Task 固定的 Skill（N-14）：读业务库的 tasks 表（AR_RELATIONAL_DSN 的 Postgres，没设时数据目录下的 SQLite，与 task-service 同一个）。"""
    key = (config.relational_dsn, config.data_dir)
    if key not in _pins:
        _pins[key] = TaskSkillPins(relational_store(config.relational_dsn, config.data_dir or Path(tempfile.mkdtemp(prefix="agentruntime-skills-"))))
    return _pins[key]


def _store_key(config: WorkerConfig) -> tuple[object, ...]:
    return (config.data_dir, config.object_store_dir, config.object_store_s3)


def _key(config: WorkerConfig) -> tuple[object, ...]:
    return (
        config.tool_workspace_root, config.tool_fetch_hosts, config.mcp_tools_file, config.tool_shell_sandbox,
        config.sandbox_backends_file, config.registry_address, config.sandbox_sessions, config.sandbox_runtime, config.preview_domain,
        *_broker_key(config), *_store_key(config),
        config.task_service_internal_url, os.environ.get('AR_TASK_SERVICE_URL',''), hashlib.sha256(config.dev_signing_key.encode()).digest(), hashlib.sha256(os.environ.get('AR_FILE_INTERNAL_TOKEN','').encode()).digest(),
    )


def _broker_key(config: WorkerConfig) -> tuple[object, ...]:
    return (
        config.tool_shell_sandbox, config.sandbox_backends_file, config.registry_address, config.data_dir, config.secrets_file, config.relational_dsn,
        config.egress_gateway_url, config.sandbox_service_address, *_store_key(config),
    )


def _relational(config: WorkerConfig) -> object:
    root = config.data_dir or Path(tempfile.mkdtemp(prefix="agentruntime-sandbox-ledger-"))
    return relational_store(config.relational_dsn, root)


def sandbox_broker(config: WorkerConfig) -> ServiceBroker | None:
    """Sandbox Service 的客户端（同样的配置得到同一个实例）；没配 AR_SANDBOX_SERVICE_ADDRESS 时为 None。调用方令牌在
    AR_SECRETS_FILE 平台作用域的 sandbox-service/tokens/worker，第一次调用时读。"""
    key = _broker_key(config)
    if key not in _brokers:
        if not config.sandbox_service_address or config.secrets_file is None:
            _brokers[key] = None
        else:
            # 工作区的记录（版本号、选用的环境配置、推送记录）在 Worker 这边读写，存档的块与清单在 context_objects 的 ObjectStore。
            archiver = WorkspaceArchiver(_relational(config), context_objects(config))
            _brokers[key] = ServiceBroker(config.sandbox_service_address, secrets_token(config.secrets_file), archiver=archiver)
    return _brokers[key]


def tenant_policies(config: WorkerConfig) -> TenantPolicies:
    """Worker 还要读的租户策略（工作区的上限、联网的范围），经 Registry；没配 Registry 时都用默认值。"""
    return TenantPolicies(registry_policy(config.registry_address) if config.registry_address else None)


def _hosted_backends(config: WorkerConfig) -> bool:
    """后端登记里有没有托管后端（没有出网网关、按域名放行，D-16）：有时也登记 request_network。登记文件归服务，这里只看种类。"""
    if config.sandbox_backends_file is None:
        return False
    try:
        doc = json.loads(Path(config.sandbox_backends_file).read_text("utf-8"))
    except (OSError, ValueError):
        return False
    return any(isinstance(b, dict) and b.get("kind") not in ("k8s", "local") for b in dict(doc.get("backends") or {}).values())


def workspace_operations(config: WorkerConfig) -> WorkspaceOperations | None:
    """Workspace API 的操作（D-06e）：在 Sandbox Service 借的环境里执行（挂卷或存档模式都一样，SBX-01）。没配服务时为 None
    （activity 报 workspace_api_unavailable）。封凭据的密钥取 SecretStore 平台作用域的 workspace/credential-key。"""
    broker = sandbox_broker(config)
    if broker is None:
        return None
    return WorkspaceOperations(
        broker, secret=lambda: _platform_secret(config, CREDENTIAL_SECRET), runtime=config.sandbox_runtime,
        allow_local_remotes=config.workspace_allow_local_remotes, telemetry=worker_telemetry(),
        grants=GrantStore(_relational(config)), policies=tenant_policies(config), workspace_root=config.tool_workspace_root,
    )


async def _platform_secret(config: WorkerConfig, name: str) -> bytes | None:
    if config.secrets_file is None:
        return None
    from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
    from agentruntime_core.errors import NotFoundError

    try:
        value = (await LocalFileSecretStoreProvider(config.secrets_file).bind_platform("sandbox").read(name)).reveal()
    except NotFoundError:
        return None
    return value.encode() if isinstance(value, str) else value


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


def _generations(config: WorkerConfig) -> WorkspaceArchiver | None:
    broker = sandbox_broker(config)
    if broker is not None:
        return broker.archiver
    if config.tool_workspace_root is None:
        return None
    return WorkspaceArchiver(_relational(config), context_objects(config))


def dev_tools(config: WorkerConfig) -> LocalToolDriverProvider:
    """开发用 ToolDriver：没配置时只有 context_read。"""
    key = _key(config)
    provider = _providers.get(key)
    if provider is None:
        provider = LocalToolDriverProvider()
        platform = PlatformToolsConfig(
            workspace_root=config.tool_workspace_root,
            fetch_hosts=config.tool_fetch_hosts,
            shell_sandbox=config.tool_shell_sandbox,
            shell_image=config.tool_shell_image,
            session_sandboxes=config.sandbox_sessions,
            shell_runtime=config.sandbox_runtime,
            network_requests=bool(config.egress_gateway_url),
            preview_domain=config.preview_domain,
            workspace_tools=bool(config.sandbox_service_address),
            hosted_backends=_hosted_backends(config),
        )
        broker = sandbox_broker(config)
        # 工作区的版本号（WS-READ-03）记在 Workspace API 用的同一个业务库里。
        generations = _generations(config)
        from agentruntime_worker.files.authority import task_attachments
        attachments = task_attachments(config)
        register_platform_tools(
            provider,
            platform,
            objects=context_objects(config),
            attachments=attachments,
            sandboxes=broker,
            telemetry=worker_telemetry(),
            generations=generations,
            grants=GrantStore(_relational(config)) if config.sandbox_service_address else None,
            policies=tenant_policies(config),
            credential_key=lambda: _platform_secret(config, envcred.SECRET_NAME),
        )
        # 交付文件（N-18，F3）：读文件与 repo_read 同一条路（F3-SANDBOX：存档模式经 Sandbox Service 读环境里的文件，挂卷读本机目录），
        # 内容进 context_objects 的 ObjectStore，元数据进业务库（task-service 读同一处）。
        files = workspace_files(platform, sandboxes=broker, telemetry=worker_telemetry(), generations=generations)
        if files is not None:
            from agentruntime_worker.tools.attachments import AttachmentImport
            spec, handler = AttachmentImport(attachments, files).tool()
            provider.register(spec, handler)
            register_delivery_tools(
                provider, objects=context_objects(config), store=relational_store(config.relational_dsn, config.data_dir or Path(tempfile.mkdtemp(prefix="agentruntime-delivery-"))),
                reader=files, limits=DeliveryLimits.from_env(os.environ),
            )
        from agentruntime_worker.files.tools import register_file_tools
        register_file_tools(provider, config)
        if config.registry_address:
            # 用户 Skill（N-14）：正文在 Registry，固定的版本在业务库的 tasks.skill_pins。
            from agentruntime_worker.registry.client import GrpcRegistry

            register_skill_tools(provider, registry=GrpcRegistry(config.registry_address), pins=skill_pins(config))
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
