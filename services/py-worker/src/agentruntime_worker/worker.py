"""Worker 启动入口：task、decision、model、program 四个任务队列（设计书 3.2、附录 K.5）。

四个池用同一份代码与镜像，只是订阅的队列不同：

    python -m agentruntime_worker --queues task,decision,model,program
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
from agentruntime_core.providers.object_store import ObjectStoreProvider

from agentruntime_worker import names
from agentruntime_worker.activities.stubs import StubActivities
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.executor.dev import data_root, dev_executor, dev_object_store, dev_secret_store
from agentruntime_worker.executor.node_executor import NodeExecutor
from agentruntime_worker.executor.tool_loop import dev_tool_loop
from agentruntime_worker.planner.activities import dev_planner
from agentruntime_worker.reviewer.activities import dev_reviewer
from agentruntime_worker.workflows.stage_runner import SubAgentWorkflow, SubgraphWorkflow
from agentruntime_worker.workflows.task import TaskWorkflow


@dataclass(frozen=True)
class QueueComponents:
    """一个队列上登记的工作流与 activity 名。local activity 在运行工作流的 Worker 上执行，登记在工作流所在的队列。"""

    workflows: tuple[str, ...] = ()
    activities: tuple[str, ...] = ()
    local_activities: tuple[str, ...] = ()


QUEUE_COMPONENTS: dict[str, QueueComponents] = {
    names.TASK_QUEUE: QueueComponents(
        workflows=(names.TASK_WORKFLOW, names.SUB_AGENT_WORKFLOW, names.SUBGRAPH_WORKFLOW),
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
    names.PROGRAM_QUEUE: QueueComponents(activities=(names.EXECUTE_PROGRAM_NODE, names.CALL_TOOL, names.RECONCILE_NODE)),
}

WORKFLOWS = {
    names.TASK_WORKFLOW: TaskWorkflow,
    names.SUB_AGENT_WORKFLOW: SubAgentWorkflow,
    names.SUBGRAPH_WORKFLOW: SubgraphWorkflow,
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
    AR_TOOL_LOOP=model 时由经 Model Gateway、用 Context Builder 组装上下文的 tool_loop 接管 tool_loop_step。"""
    activities = (stubs or StubActivities(config)).activities()
    if config.planner == "model":
        activities.update(dev_planner(config).activities())
    if config.reviewer == "model":
        activities.update(dev_reviewer(config).activities())
    if config.tool_loop == "model":
        activities.update(dev_tool_loop(config).activities())
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
    client = await connect(config)
    async with contextlib.AsyncExitStack() as stack:
        for worker in build_workers(client, config, queues):
            await stack.enter_async_context(worker)
        await asyncio.Event().wait()


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
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(config, queues))
    return 0
