"""测试共用：仓库路径、/schemas 校验器、Task 工作流的输入。"""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path
from typing import Any

from google.protobuf import struct_pb2, timestamp_pb2
from jsonschema import Draft202012Validator
from jsonschema_specifications import REGISTRY as SPECIFICATIONS
from referencing import Resource

from agentruntime.common.v1 import enums_pb2, tenant_pb2
from agentruntime.workflow.v1 import task_pb2

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMAS_DIR = REPO_ROOT / "schemas"
WORKER_DIR = REPO_ROOT / "services" / "py-worker"
HISTORIES_DIR = WORKER_DIR / "tests" / "histories"

SESSION_ID = "ses_01J" + "0" * 22 + "S"


@cache
def _registry():
    resources = []
    for path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
        schema = json.loads(path.read_text("utf-8"))
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return SPECIFICATIONS.with_resources(resources)


def validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS_DIR / f"{name}.schema.json").read_text("utf-8"))
    return Draft202012Validator(schema, registry=_registry(), format_checker=Draft202012Validator.FORMAT_CHECKER)


def assert_valid(name: str, value: Any) -> None:
    errors = sorted(validator(name).iter_errors(value), key=lambda e: list(e.absolute_path))
    assert not errors, f"{name}.schema.json 校验失败：\n" + "\n".join(
        f"  {list(e.absolute_path)}: {e.message}" for e in errors
    )


_counter = 0


def new_task_id() -> str:
    """每次一个新的、合乎格式的 task_id；带进程号，同一开发服务上多次运行测试也不重复。"""
    global _counter
    _counter += 1
    return f"tsk_01J{os.getpid() % 100000:05d}{_counter:018d}"


def task_input(
    task_id: str | None = None,
    *,
    stub_plan: str = "answer",
    stub_rounds: int = 1,
    message: str = "上季度退款率为什么上升？",
    done_when: str | None = None,
    pinned: task_pb2.PinnedVersions | None = None,
    scenario: str | None = None,
    extra_inputs: dict | None = None,
    effect_ceiling: int = enums_pb2.EFFECT_READ,
    depth: int = 0,
    budget: task_pb2.Budget | None = None,
) -> task_pb2.TaskWorkflowInput:
    inputs = struct_pb2.Struct()
    inputs.update({"stub_plan": stub_plan, "stub_rounds": stub_rounds})
    if done_when is not None:
        inputs.update({"done_when": done_when})
    if scenario is not None:
        inputs.update({"scenario": scenario})
    if extra_inputs:
        inputs.update(extra_inputs)
    submitted = timestamp_pb2.Timestamp()
    submitted.FromSeconds(1_790_000_000)
    return task_pb2.TaskWorkflowInput(
        tenant=tenant_pb2.TenantContext(tenant_id="t1", user_id="u1", session_id=SESSION_ID),
        task_id=task_id or new_task_id(),
        pinned_versions=pinned
        or task_pb2.PinnedVersions(
            recipe="stub@v1",
            node_templates={"stub_answer": "v2"},
            reasoning_templates={"direct": "v1"},
            model_profiles={"mock/model": "v3"},
            tenant_policy="v5",
        ),
        message=message,
        inputs=inputs,
        submitted_at=submitted,
        session_access=enums_pb2.SESSION_ACCESS_READ_WRITE,
        priority=enums_pb2.PRIORITY_INTERACTIVE,
        budget=budget if budget is not None else task_pb2.Budget(usd_micros=2_000_000, tool_calls=20),
        effect_ceiling=effect_ceiling,
        depth=depth,
    )
