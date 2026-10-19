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
from agentruntime_worker.worker import QUEUE_COMPONENTS, build_workers, parse_queues


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
    assert set(activities) == set(StubActivities(WorkerConfig()).activities())


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


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_build_workers_registers_components_per_queue(temporal_env: WorkflowEnvironment) -> None:
    workers = build_workers(temporal_env.client, WorkerConfig(), [names.DECISION_QUEUE, names.TASK_QUEUE])
    assert [w.task_queue for w in workers] == [names.DECISION_QUEUE, names.TASK_QUEUE]


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
