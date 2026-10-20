"""开发环境的 Node Executor：依赖全部用 S-02 的开发实现，模型用桩（StubModelGateway）。

数据放在 WorkerConfig.data_dir（没配时用临时目录）：objects/（ObjectStore）、secrets.json（SecretStore）、sandbox/。
ObjectStore 与 SecretStore 的位置可以单独配置（AR_OBJECT_STORE_DIR、AR_SECRETS_FILE），与 task-service 共用，
Claim-Check（S-06）卸载的载荷两边才能互相取回。
human 节点的问题打印到标准错误；回复由 TemporalReplySink 以 human_reply 信号送回 Task 工作流（附录 K.1）。
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

from google.protobuf import timestamp_pb2
from temporalio.client import Client

from agentruntime.workflow.v1 import task_pb2
from agentruntime_core.dev.cache import InMemoryCacheProvider
from agentruntime_core.dev.human_channel import CliHumanChannelProvider
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.sandbox import LocalSubprocessSandboxProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.providers.human_channel import Reply
from agentruntime_core.providers.object_store import ObjectStoreProvider

from agentruntime_worker import names
from agentruntime_worker.activities.stubs import StubModelGateway
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.executor.node_executor import ExecutorConfig, ExecutorDeps, NodeExecutor, workflow_question_id
from agentruntime_worker.executor.schemas import DictSchemaResolver

logger = logging.getLogger(__name__)


class TemporalReplySink:
    """HumanChannel 的回复去向：给 Task 工作流发 human_reply 信号。"""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def deliver(self, reply: Reply) -> None:
        replied_at = timestamp_pb2.Timestamp()
        replied_at.FromDatetime(reply.replied_at)
        await self._client.get_workflow_handle(reply.task_id).signal(
            names.HUMAN_REPLY_SIGNAL,
            task_pb2.HumanReplySignal(
                question_id=workflow_question_id(reply.question_id),
                option=reply.option,
                guidance=reply.guidance,
                replied_by_user_id=reply.replied_by_user_id,
                replied_at=replied_at,
            ),
        )


class _LoggingSink:
    """没有 Temporal 客户端时（例如单元测试）只记日志。"""

    async def deliver(self, reply: Reply) -> None:
        logger.warning("没有 Temporal 客户端，丢弃人工回复：%s", reply.question_id)


def data_root(config: WorkerConfig) -> Path:
    root = config.data_dir or Path(tempfile.mkdtemp(prefix="agentruntime-worker-"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def dev_object_store(config: WorkerConfig, root: Path) -> LocalDiskObjectStoreProvider:
    """ObjectStore 的开发实现，Node Executor 的大结果与 Claim-Check 共用（与 task-service 指向同一个目录）。"""
    objects, _ = config.store_paths(root)
    return LocalDiskObjectStoreProvider(objects, signing_key=os.urandom(32))


def dev_secret_store(config: WorkerConfig, root: Path) -> LocalFileSecretStoreProvider:
    _, secrets = config.store_paths(root)
    return LocalFileSecretStoreProvider(secrets)


def dev_executor(
    config: WorkerConfig,
    *,
    client: Client | None = None,
    executor_config: ExecutorConfig | None = None,
    objects: ObjectStoreProvider | None = None,
) -> NodeExecutor:
    root = data_root(config)
    sink = TemporalReplySink(client) if client is not None else _LoggingSink()
    deps = ExecutorDeps(
        tools=LocalToolDriverProvider(),
        objects=objects or dev_object_store(config, root),
        secrets=dev_secret_store(config, root),
        cache=InMemoryCacheProvider(),
        humans=CliHumanChannelProvider(sink, output=sys.stderr),
        telemetry=ConsoleTelemetryProvider(stream=sys.stderr),
        models=StubModelGateway(config),
        schemas=DictSchemaResolver({}),
        sandbox=LocalSubprocessSandboxProvider(root / "sandbox"),
    )
    return NodeExecutor(deps, executor_config)
