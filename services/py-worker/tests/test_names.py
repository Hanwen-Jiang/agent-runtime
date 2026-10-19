"""S-03 验收：Python 的 Task 工作流与 Go 的 Session 工作流靠同一组名字对上，名字只在契约里定义一次
（names.proto 的 wire_name），这里逐个比对。Go 侧见 internal/session/names_test.go。"""

from __future__ import annotations

import json

import pytest

from agentruntime.common.v1 import options_pb2
from agentruntime.workflow.v1 import names_pb2

from agentruntime_worker import names

from .support import SCHEMAS_DIR


def wire_name(enum: str, value: str) -> str:
    descriptor = names_pb2.DESCRIPTOR.enum_types_by_name[enum]
    if value not in descriptor.values_by_name:
        pytest.fail(f"契约中还没有 {enum}.{value}：需要按 S-01 流程加入 names.proto", pytrace=False)
    options = descriptor.values_by_name[value].GetOptions()
    name = options.Extensions[options_pb2.wire_name]
    assert name, f"{enum}.{value} 没有 wire_name"
    return name


@pytest.mark.parametrize(
    ("enum", "value", "actual"),
    [
        ("WorkflowType", "WORKFLOW_TYPE_SESSION", names.SESSION_WORKFLOW),
        ("WorkflowType", "WORKFLOW_TYPE_TASK", names.TASK_WORKFLOW),
        ("SignalName", "SIGNAL_NAME_HUMAN_REPLY", names.HUMAN_REPLY_SIGNAL),
        ("SignalName", "SIGNAL_NAME_TASK_FINISHED", names.TASK_FINISHED_SIGNAL),
        ("QueryName", "QUERY_NAME_TASK_STATE", names.TASK_STATE_QUERY),
        ("TaskQueue", "TASK_QUEUE_TASK", names.TASK_QUEUE),
        ("TaskQueue", "TASK_QUEUE_DECISION", names.DECISION_QUEUE),
        ("TaskQueue", "TASK_QUEUE_MODEL", names.MODEL_QUEUE),
        ("TaskQueue", "TASK_QUEUE_PROGRAM", names.PROGRAM_QUEUE),
    ],
)
def test_names_match_contract(enum: str, value: str, actual: str) -> None:
    assert actual == wire_name(enum, value)


def test_task_state_schema_id_matches_schema_file() -> None:
    schema = json.loads((SCHEMAS_DIR / "task_state.schema.json").read_text("utf-8"))
    assert names.TASK_STATE_SCHEMA_ID == schema["$id"]
