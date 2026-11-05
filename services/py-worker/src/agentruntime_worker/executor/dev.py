"""开发环境的 Node Executor：依赖全部用 S-02 的开发实现；模型经 model_gateway(config)：设了 AR_MODEL_GATEWAY_ADDRESS
时是到 go-model-gateway 的 GrpcModelGateway，没设时是桩（StubModelGateway）。

数据放在 WorkerConfig.data_dir（没配时用临时目录）：objects/（ObjectStore）、secrets.json（SecretStore）、sandbox/。
ObjectStore 与 SecretStore 的位置可以单独配置（AR_OBJECT_STORE_DIR、AR_SECRETS_FILE），与 task-service 共用，
Claim-Check（S-06）卸载的载荷两边才能互相取回。
human 节点的问题打印到标准错误；回复由 TemporalReplySink 以 human_reply 信号送回 Task 工作流（附录 K.1）。
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from google.protobuf import timestamp_pb2
from temporalio.client import Client

from agentruntime.workflow.v1 import task_pb2
from agentruntime_core.dev.cache import InMemoryCacheProvider
from agentruntime_core.dev.human_channel import CliHumanChannelProvider
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.sandbox import LocalSubprocessSandboxProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.providers.human_channel import Reply
from agentruntime_core.providers.object_store import ObjectStoreProvider
from agentruntime_core.providers.sandbox import SandboxProvider

from agentruntime_worker import names
from agentruntime_worker.activities.stubs import StubModelGateway
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.executor.deltas import DeltaPusher, secret_token
from agentruntime_worker.executor.grpc_gateway import GrpcModelGateway
from agentruntime_worker.executor.model_gateway import ModelGateway
from agentruntime_worker.executor.node_executor import ExecutorConfig, ExecutorDeps, NodeExecutor, workflow_question_id
from agentruntime_worker.executor.schemas import DictSchemaResolver
from agentruntime_worker.context.builder import ContextBuilder, ContextConfig
from agentruntime_worker.session.transcript import session_transcript
from agentruntime_worker.telemetry import worker_telemetry
from agentruntime_worker.tools import dev_tools, sandbox_broker

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


_opened: contextvars.ContextVar[list[GrpcModelGateway] | None] = contextvars.ContextVar("opened_model_gateways", default=None)


def model_gateway(config: WorkerConfig) -> ModelGateway:
    """Node Executor、Planner、Reviewer、tool_loop 共用的模型出口：设了 AR_MODEL_GATEWAY_ADDRESS 时是 GrpcModelGateway，
    否则是桩模型。在 closing_model_gateways() 里组装时，同一地址只建一个客户端，退出时统一关闭。"""
    if not config.model_gateway_address:
        return StubModelGateway(config)
    opened = _opened.get()
    for gateway in opened or ():
        if gateway.address == config.model_gateway_address:
            return gateway
    gateway = GrpcModelGateway(config.model_gateway_address)
    if opened is not None:
        opened.append(gateway)
    return gateway


@contextlib.asynccontextmanager
async def closing_model_gateways() -> AsyncIterator[list[GrpcModelGateway]]:
    """在这里面经 model_gateway() 建的 GrpcModelGateway 都记下来，退出时（包括被取消）逐个关闭。"""
    opened: list[GrpcModelGateway] = []
    token = _opened.set(opened)
    try:
        yield opened
    finally:
        _opened.reset(token)
        for gateway in opened:
            await gateway.aclose()


def data_root(config: WorkerConfig) -> Path:
    root = config.data_dir or Path(tempfile.mkdtemp(prefix="agentruntime-worker-"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def dev_object_store(config: WorkerConfig, root: Path) -> ObjectStoreProvider:
    """Node Executor 的大结果与 Claim-Check 共用的 ObjectStore（与 task-service 指向同一处）：设了 AR_S3_ENDPOINT 时是
    S3 兼容的对象存储（R2、S3、MinIO；D-09），否则是开发实现（AR_OBJECT_STORE_DIR）。"""
    if config.object_store_s3 is not None:
        return config.object_store_s3.provider()
    objects, _ = config.store_paths(root)
    return LocalDiskObjectStoreProvider(objects, signing_key=os.urandom(32))


def dev_secret_store(config: WorkerConfig, root: Path) -> LocalFileSecretStoreProvider:
    _, secrets = config.store_paths(root)
    return LocalFileSecretStoreProvider(secrets)


def dev_sandbox(config: WorkerConfig, root: Path) -> SandboxProvider:
    """内置模板 sandbox_exec 用的 Sandbox：配了 Sandbox Service（AR_TOOL_SHELL_SANDBOX=sandbox）时经它借一次性的环境（SBX-01），
    否则是本机子进程（只给开发与测试）。"""
    if config.tool_shell_sandbox == "sandbox":
        broker = sandbox_broker(config)
        if broker is not None:
            from agentruntime_worker.sandbox.client import ServiceSandboxProvider

            return ServiceSandboxProvider(broker)  # type: ignore[return-value]
    return LocalSubprocessSandboxProvider(root / "sandbox")


def dev_executor(
    config: WorkerConfig,
    *,
    client: Client | None = None,
    executor_config: ExecutorConfig | None = None,
    objects: ObjectStoreProvider | None = None,
) -> NodeExecutor:
    root = data_root(config)
    sink = TemporalReplySink(client) if client is not None else _LoggingSink()
    model_gateway_instance = model_gateway(config)
    object_store = objects or dev_object_store(config, root)
    deps = ExecutorDeps(
        tools=dev_tools(config),
        objects=object_store,
        secrets=dev_secret_store(config, root),
        cache=InMemoryCacheProvider(),
        humans=CliHumanChannelProvider(sink, output=sys.stderr),
        telemetry=worker_telemetry(),
        models=model_gateway_instance,
        schemas=DictSchemaResolver({}),
        sandbox=dev_sandbox(config, root),
        transcript=session_transcript(config.relational_dsn, config.data_dir, object_store),
        stream_deliver=config.stream_deliver,
        # 逐字增量（DELTA）：给了 task-service 的内部端口才推。
        deltas=DeltaPusher(config.task_service_internal_url, secret_token(config.store_paths(root)[1], config.delta_caller))
        if config.task_service_internal_url else None,
        # 桩模型按固定 JSON 提示协议解释输入；真实 Gateway 才使用 ContextBuilder。
        context_builder=ContextBuilder(
            model_gateway_instance,
            object_store,
            ContextConfig(effective_tokens=config.context_effective_tokens, offload_tokens=config.context_offload_tokens),
        ) if config.model_gateway_address else None,
    )
    return NodeExecutor(deps, executor_config or ExecutorConfig(context_effective_tokens=config.context_effective_tokens))
