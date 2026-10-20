"""S-03 验收：task、decision、model、program 四个任务队列的 Worker 启动入口（计划 S-03 交付；附录 K.5）。"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from temporalio.testing import WorkflowEnvironment

from agentruntime.common.v1 import options_pb2
from agentruntime.workflow.v1 import names_pb2

from agentruntime_worker import names
from agentruntime_worker.activities.stubs import StubActivities
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.executor.dev import dev_executor
from agentruntime_worker.worker import QUEUE_COMPONENTS, build_workers, decision_activities, parse_queues


def contract_queues() -> set[str]:
    enum = names_pb2.DESCRIPTOR.enum_types_by_name["TaskQueue"]
    return {v.GetOptions().Extensions[options_pb2.wire_name] for v in enum.values if v.number != 0}


def test_worker_serves_the_four_python_queues() -> None:
    assert set(QUEUE_COMPONENTS) == {names.TASK_QUEUE, names.DECISION_QUEUE, names.MODEL_QUEUE, names.PROGRAM_QUEUE}
    # session 是 Go 的队列，offline 属于 Learning Pipeline；其余都要有人消费。
    assert contract_queues() - set(QUEUE_COMPONENTS) == {"session", "offline"}


def test_every_component_is_registered_on_exactly_one_queue() -> None:
    activities = [a for c in QUEUE_COMPONENTS.values() for a in c.activities + c.local_activities]
    workflows = [w for c in QUEUE_COMPONENTS.values() for w in c.workflows]
    assert sorted(activities) == sorted(set(activities))
    assert workflows == [names.TASK_WORKFLOW, names.SUB_AGENT_WORKFLOW, names.SUBGRAPH_WORKFLOW]
    # 决策类的桩来自 StubActivities，执行 Node 的 activity 来自 Node Executor，两边不重复。
    stub_names = set(StubActivities(WorkerConfig()).activities())
    executor_names = set(dev_executor(WorkerConfig()).activities())
    assert not stub_names & executor_names
    assert set(activities) == stub_names | executor_names
    assert {names.ASK_HUMAN, names.CANCEL_HUMAN} <= set(QUEUE_COMPONENTS[names.DECISION_QUEUE].activities)
    assert names.RECONCILE_NODE in QUEUE_COMPONENTS[names.PROGRAM_QUEUE].activities


def test_local_activities_live_with_the_workflows() -> None:
    """local activity 在运行工作流的 Worker 上执行，必须和工作流登记在同一个队列。"""
    for queue, components in QUEUE_COMPONENTS.items():
        if components.local_activities:
            assert components.workflows, f"{queue} 上有 local activity 却没有工作流"
    assert QUEUE_COMPONENTS[names.TASK_QUEUE].local_activities == (names.RUN_HOOKS,)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("task,decision,model,program", ["task", "decision", "model", "program"]),
        ("model", ["model"]),
        (" decision , task ,decision", ["decision", "task"]),
    ],
)
def test_parse_queues(value: str, expected: list[str]) -> None:
    assert parse_queues(value) == expected


@pytest.mark.parametrize("value", ["", "session", "task,offline", "gpu"])
def test_parse_queues_rejects_unknown_queues(value: str) -> None:
    with pytest.raises(ValueError):
        parse_queues(value)


def test_the_model_planner_takes_over_plan_round_only_when_asked() -> None:
    """S-09：AR_PLANNER=model 时 plan_round 换成真正的 Planner，其余决策类 activity 仍是桩。"""
    from agentruntime_worker.activities.stubs import StubActivities
    from agentruntime_worker.planner.activities import PlannerActivities

    default = decision_activities(WorkerConfig())
    model = decision_activities(WorkerConfig(planner="model"))
    assert isinstance(default[names.PLAN_ROUND].__self__, StubActivities)
    assert isinstance(model[names.PLAN_ROUND].__self__, PlannerActivities)
    assert set(model) == set(default)
    assert isinstance(model[names.REVIEW_ROUND].__self__, StubActivities)


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_build_workers_registers_components_per_queue(temporal_env: WorkflowEnvironment) -> None:
    workers = build_workers(temporal_env.client, WorkerConfig(), [names.DECISION_QUEUE, names.TASK_QUEUE])
    assert [w.task_queue for w in workers] == [names.DECISION_QUEUE, names.TASK_QUEUE]


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_worker_client_encodes_payloads_through_claim_check(temporal_env: WorkflowEnvironment, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """S-06：Worker 的客户端带 Claim-Check（阈值取自配置，租户按工作流 memo 确定）；回放工具用同一份配置。"""
    from agentruntime_core.adapters import claim_check as cc

    from agentruntime_worker.worker import worker_data_converter

    config = WorkerConfig(data_dir=tmp_path, claim_check_threshold=4096)
    converter = worker_data_converter(config, temporal_env.client)
    codec = converter.payload_codec
    assert isinstance(codec, cc.ClaimCheckCodec)
    assert isinstance(codec.resolver, cc.WorkflowMemoResolver)
    assert codec.claim_check.threshold == 4096


def test_module_entry_point_prints_usage() -> None:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-m", "agentruntime_worker", "--help"], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "--queues" in proc.stdout


def test_module_entry_point_rejects_unknown_queue() -> None:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "agentruntime_worker", "--queues", "gpu"], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60
    )
    assert proc.returncode == 2
