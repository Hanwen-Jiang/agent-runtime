"""Worker 启动入口：task、decision、model、program 四个任务队列（设计书 3.2、附录 K.5）。

四个池用同一份代码与镜像，只是订阅的队列不同：

    python -m agentruntime_worker --queues task,decision,model,program,workspace

沙箱的借还、对账与按项目构建环境都在 Sandbox Service 里（SBX-01）。原来的对账工作流与构建工作流照旧登记（Temporal 里可能还有
它们的运行），activity 换成 sandbox.retired 里的；订阅 task 队列的 Worker 启动时结束还在跑的对账工作流。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from temporalio.client import Client
from temporalio.converter import DataConverter
from temporalio.worker import Worker

from agentruntime_core.adapters.claim_check import ClaimCheck, WorkflowMemoResolver
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.providers.object_store import ObjectStoreProvider

from agentruntime_worker import names
from agentruntime_worker.activities.stubs import StubActivities
from agentruntime_worker.activities.types import StagePolicy
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.executor.dev import closing_model_gateways, data_root, dev_executor, dev_object_store, dev_secret_store
from agentruntime_worker.executor.node_executor import NodeExecutor
from agentruntime_worker.executor.tool_loop import dev_tool_loop
from agentruntime_worker.planner.activities import dev_planner
from agentruntime_worker.policy import PolicyEngine
from agentruntime_worker.registry import RegistryCatalog, RegistryStagePolicy, closing_registries, registry_client
from agentruntime_worker.reviewer.activities import dev_reviewer
from agentruntime_worker.telemetry import worker_telemetry
from agentruntime_worker.sandbox.retired import RetiredSandboxActivities
from agentruntime_worker.tools import DevCatalog, ToolCatalog, dev_tools, mcp_leases, sandbox_broker, tool_sessions, workspace_operations
from agentruntime_worker.workflows.env_build import EnvironmentBuildWorkflow
from agentruntime_worker.workflows.sandbox_reconcile import SandboxReconcileWorkflow
from agentruntime_worker.workflows.stage_runner import SubAgentWorkflow, SubgraphWorkflow
from agentruntime_worker.workflows.task import TaskWorkflow
from agentruntime_worker.workflows.workspace import WorkspaceOperationWorkflow
from agentruntime_worker.workspace.operations import WorkspaceActivities


@dataclass(frozen=True)
class QueueComponents:
    """一个队列上登记的工作流与 activity 名。local activity 在运行工作流的 Worker 上执行，登记在工作流所在的队列。"""

    workflows: tuple[str, ...] = ()
    activities: tuple[str, ...] = ()
    local_activities: tuple[str, ...] = ()


QUEUE_COMPONENTS: dict[str, QueueComponents] = {
    names.TASK_QUEUE: QueueComponents(
        workflows=(
            names.TASK_WORKFLOW, names.SUB_AGENT_WORKFLOW, names.SUBGRAPH_WORKFLOW, names.SANDBOX_RECONCILE_WORKFLOW, names.ENV_BUILD_WORKFLOW,
            names.WORKSPACE_OPERATION_WORKFLOW,
        ),
        local_activities=(names.RUN_HOOKS,),
    ),
    names.DECISION_QUEUE: QueueComponents(
        activities=(
            names.PLAN_ROUND,
            names.REVIEW_ROUND,
            names.FINALIZE_TASK,
            names.LOAD_STAGE_POLICY,
            names.RESOLVE_CATALOG,
            names.EXPAND_TEMPLATE,
            names.ASK_HUMAN,
            names.CANCEL_HUMAN,
        )
    ),
    names.MODEL_QUEUE: QueueComponents(activities=(names.EXECUTE_MODEL_NODE, names.TOOL_LOOP_STEP)),
    names.PROGRAM_QUEUE: QueueComponents(
        activities=(
            names.EXECUTE_PROGRAM_NODE, names.CALL_TOOL, names.RECONCILE_NODE, names.RECONCILE_SANDBOXES, names.BUILD_ENVIRONMENT,
            names.WORKSPACE_OPERATION,
        )
    ),
    # WS-READ-02：task-service 新起的 Workspace 操作在这里，工作流与 activity 同一个队列。task、program 上照旧登记它们，
    # 给改之前起的、还在跑的操作；那些跑完之后再从旧队列去掉。
    names.WORKSPACE_QUEUE: QueueComponents(workflows=(names.WORKSPACE_OPERATION_WORKFLOW,), activities=(names.WORKSPACE_OPERATION,)),
}

WORKFLOWS = {
    names.TASK_WORKFLOW: TaskWorkflow,
    names.SUB_AGENT_WORKFLOW: SubAgentWorkflow,
    names.SUBGRAPH_WORKFLOW: SubgraphWorkflow,
    names.SANDBOX_RECONCILE_WORKFLOW: SandboxReconcileWorkflow,
    names.ENV_BUILD_WORKFLOW: EnvironmentBuildWorkflow,
    names.WORKSPACE_OPERATION_WORKFLOW: WorkspaceOperationWorkflow,
}


def parse_queues(value: str) -> list[str]:
    """解析 --queues：逗号分隔、去重保序；未知队列抛 ValueError。"""
    queues: list[str] = []
    for part in value.split(","):
        queue = part.strip()
        if not queue:
            continue
        if queue not in QUEUE_COMPONENTS:
            raise ValueError(f"不认识的队列 {queue}，可选：{', '.join(QUEUE_COMPONENTS)}")
        if queue not in queues:
            queues.append(queue)
    if not queues:
        raise ValueError("至少要订阅一个队列")
    return queues


def worker_data_converter(config: WorkerConfig, raw_client: Client, *, objects: ObjectStoreProvider | None = None) -> DataConverter:
    """Worker 客户端的 DataConverter：默认的载荷转换加上 Claim-Check（S-06）。

    租户按工作流 memo 里的作用域确定（WorkflowMemoResolver，describe 用不带 Claim-Check 的 raw_client）。
    ObjectStore 与 SecretStore 与 task-service 指向同一处；objects 可以换成别的实现（例如故障注入的包装）。
    """
    root = data_root(config)
    claim_check = ClaimCheck(objects or dev_object_store(config, root), dev_secret_store(config, root), threshold=config.claim_check_threshold)
    return dataclasses.replace(DataConverter.default, payload_codec=claim_check.codec(WorkflowMemoResolver(raw_client)))


async def connect(config: WorkerConfig, *, objects: ObjectStoreProvider | None = None) -> Client:
    """连上 Temporal，返回带 Claim-Check 的客户端。"""
    raw = await Client.connect(config.temporal_address, namespace=config.namespace)
    return Client(raw.service_client, namespace=raw.namespace, data_converter=worker_data_converter(config, raw, objects=objects))


def decision_activities(config: WorkerConfig, stubs: StubActivities | None = None) -> dict[str, Any]:
    """决策类 activity：桩，或者 AR_PLANNER=model 时由真正的 Planner 接管 plan_round（S-09），
    AR_REVIEWER=model 时由真正的 Reviewer 与收尾写回接管 review_round 与 finalize_task（S-10），
    AR_TOOL_LOOP=model 时由经 Model Gateway、用 Context Builder 组装上下文的 tool_loop 接管 tool_loop_step，
    resolve_catalog 的工具 effect 取自开发用 ToolDriver 的登记（T-10；没登记的工具不给，Stage Runner 当作 write_irreversible），
    设了 AR_REGISTRY_ADDRESS 时 resolve_catalog 的模板改按 Task 固定的版本查 Registry（T-08），
    load_stage_policy 改读 Registry 里租户策略与 Recipe 声明的 Hook（T-09）。run_hooks 一律是真的 Policy Engine（T-09）。"""
    activities = (stubs or StubActivities(config)).activities()
    tools = ToolCatalog(dev_tools(config), mcp_leases(config))
    activities.update(DevCatalog(tools).activities())
    if config.planner == "model":
        activities.update(dev_planner(config).activities())
    if config.reviewer == "model":
        activities.update(dev_reviewer(config).activities())
    if config.tool_loop == "model":
        activities.update(dev_tool_loop(config).activities())
    if config.registry_address:
        registry = registry_client(config)
        activities.update(RegistryCatalog(registry, tools=tools).activities())
        activities.update(RegistryStagePolicy(registry, defaults=StagePolicy()).activities())
    # Hook 一律由真的 Policy Engine 执行（T-09；代替 S-04 的桩）。审计与 trace 的记录经 worker_telemetry()：
    # 设了 OTEL_EXPORTER_OTLP_ENDPOINT 时发给 Collector，否则写标准错误（T-15）。
    activities.update(PolicyEngine(telemetry=worker_telemetry()).activities())
    return activities


def build_workers(
    client: Client,
    config: WorkerConfig,
    queues: Sequence[str],
    stubs: StubActivities | None = None,
    executor: NodeExecutor | None = None,
) -> list[Worker]:
    """每个队列一个 Worker，按 QUEUE_COMPONENTS 登记：决策类见 decision_activities，执行 Node 用 Node Executor。"""
    activities = {
        **decision_activities(config, stubs),
        **(executor or dev_executor(config, client=client)).activities(),
        **RetiredSandboxActivities().activities(),
        **WorkspaceActivities(workspace_operations(config)).activities(),
    }
    workers = []
    for queue in queues:
        components = QUEUE_COMPONENTS[queue]
        workers.append(
            Worker(
                client,
                task_queue=queue,
                workflows=[WORKFLOWS[w] for w in components.workflows],
                activities=[activities[a] for a in components.activities + components.local_activities],
            )
        )
    return workers


async def serve(config: WorkerConfig, queues: Sequence[str]) -> None:
    """连上 Temporal，每个队列起一个 Worker，一直运行到被取消（Ctrl+C 时 asyncio.run 取消它）。

    退出时（包括被取消、Worker 出错）关闭本进程为 Node Executor、Planner、Reviewer、tool_loop 创建的 Model Gateway 客户端
    （GrpcModelGateway.aclose；同一地址共用一个）、Registry 客户端（GrpcRegistry.aclose）、MCP 会话（tool_sessions，T-10）与
    Sandbox Service 的连接；桩模型 StubModelGateway 没有要关的东西。
    """
    client = await connect(config)
    async with closing_model_gateways(), closing_registries(), tool_sessions(config), contextlib.AsyncExitStack() as stack:
        for worker in build_workers(client, config, queues):
            await stack.enter_async_context(worker)
        broker = sandbox_broker(config)
        if broker is not None:
            stack.push_async_callback(broker.aclose)
        if names.TASK_QUEUE in queues:
            await retire_sandbox_reconcile(client)
        await asyncio.Event().wait()


async def retire_sandbox_reconcile(client: Client, *, workflow_id: str = names.SANDBOX_RECONCILE_WORKFLOW_ID) -> bool:
    """结束还在跑的对账工作流（SBX-01 之前由 Worker 起；对账已经搬进 Sandbox Service）。没有在跑的返回 False。"""
    from temporalio.service import RPCError, RPCStatusCode

    try:
        await client.get_workflow_handle(workflow_id).terminate(reason="沙箱的对账已经搬进 Sandbox Service（SBX-01）")
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            return False
        raise
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agentruntime_worker", description="Agent Runtime 的 Python Worker")
    parser.add_argument(
        "--queues",
        default=",".join(QUEUE_COMPONENTS),
        help=f"要订阅的任务队列，逗号分隔；默认全部：{','.join(QUEUE_COMPONENTS)}",
    )
    args = parser.parse_args(argv)
    try:
        queues = parse_queues(args.queues)
        config = WorkerConfig.from_env(os.environ)
    except ValueError as err:
        parser.error(str(err))
    if config.secrets_seed_file is not None and config.secrets_file is not None:
        # 先并种子，后面各处建的 SecretStore 读到的就是并好的文件。
        LocalFileSecretStoreProvider(config.secrets_file).merge_seed(config.secrets_seed_file)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(config, queues))
    return 0
