"""Unit tests for pre-execution JSON Schema validation and 1-turn field feedback."""

from __future__ import annotations

from typing import Any

import pytest
from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import enums_pb2, metering_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.errors import InvalidError
from agentruntime_worker.activities.types import Tenant, ToolLoopStepInput
from agentruntime_worker.context.builder import ContextBuilder, ContextConfig
from agentruntime_worker.executor.tool_loop import ModelToolLoop
from agentruntime_worker.executor.tool_schema import format_schema_correction, validate_tool_arguments


def _struct(data: dict[str, Any]) -> struct_pb2.Struct:
    s = struct_pb2.Struct()
    json_format.ParseDict(data, s)
    return s


def test_validate_tool_arguments_catches_missing_required_type_enum_and_additional() -> None:
    schema = {
        "type": "object",
        "required": ["path", "limit"],
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "limit": {"type": "integer"},
            "mode": {"type": "string", "enum": ["read", "write"]},
        },
        "additionalProperties": False,
    }
    assert validate_tool_arguments("repo_read", {"path": "README.md", "limit": 10.0, "mode": "read"}, schema) == []

    errs = validate_tool_arguments("repo_read", {"limit": "ten", "mode": "exec", "extra": 1}, schema)
    assert any("path" in e for e in errs)
    assert any("limit" in e for e in errs)
    assert any("mode" in e for e in errs)
    assert any("extra" in e for e in errs)
    msg = format_schema_correction("repo_read", errs)
    assert "repo_read" in msg and "path" in msg


class _StubObjects:
    def bind(self, _tenant: Any) -> Any:
        return self


class _FakeToolDriver:
    def bind(self, _tenant: Any) -> Any:
        return self

    async def list_tools(self) -> list[Any]:
        from datetime import timedelta
        from agentruntime_core.providers.tool_driver import Effect, ToolSpec

        return [
            ToolSpec(
                name="repo_read",
                version="v1",
                effect=Effect.READ,
                description="Read file lines",
                input_schema={
                    "type": "object",
                    "required": ["path", "start_line"],
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "start_line": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                permissions=frozenset(),
                timeout=timedelta(seconds=5),
            )
        ]


class _FakeGateway:
    def __init__(self, scripted: list[dict[str, Any]]) -> None:
        self.scripted = list(scripted)
        self.calls: list[mg.GenerateRequest] = []

    async def generate(self, req: mg.GenerateRequest) -> mg.GenerateResponse:
        self.calls.append(req)
        item = self.scripted.pop(0)
        return mg.GenerateResponse(
            model=mg.ModelChoice(model_profile_id="test/mock", tier="strong@medium", vendor="openai", context_window=128000),
            usage=metering_pb2.Usage(input_tokens=100, output_tokens=20, cost_usd_micros=item.get("cost_micros", 1000)),
            tool_calls=[
                mg.ToolCall(id="c1", name=item["tool"], arguments=_struct(item["args"]))
            ],
        )


@pytest.mark.asyncio
async def test_model_tool_loop_repairs_bad_tool_schema_with_field_feedback(tmp_path: Any) -> None:
    from agentruntime_worker.tools.catalog import ToolCatalog

    gateway = _FakeGateway([
        {"tool": "repo_read", "args": {"start_line": "first"}, "cost_micros": 1200},
        {"tool": "repo_read", "args": {"path": "README.md", "start_line": 1}, "cost_micros": 1500},
    ])
    builder = ContextBuilder(gateway, LocalDiskObjectStoreProvider(tmp_path / "obj", signing_key=b"k"), ContextConfig())  # type: ignore[arg-type]
    loop = ModelToolLoop(gateway, builder, tools=ToolCatalog(_FakeToolDriver()))  # type: ignore[arg-type]
    inp = ToolLoopStepInput(
        tenant=Tenant(tenant_id="t1", workspace_id="w1"),
        task_id="task-schema-1",
        round_seq=1,
        step_id="step-1",
        iteration=1,
        node={"id": "read", "tools": ["repo_read"]},
        history=[],
        task_state={"goal": "read readme"},
        task_plan={"goal": "read readme", "steps": []},
        task_family="general.coding",
    )
    out = await loop.step(inp)
    assert out.action == "tool"
    assert out.args == {"path": "README.md", "start_line": 1}
    assert len(gateway.calls) == 2
    correction_msg = gateway.calls[1].generation.messages[-1].text
    assert "path" in correction_msg and "start_line" in correction_msg
    assert out.cost_usd == pytest.approx(0.0027)


@pytest.mark.asyncio
async def test_model_tool_loop_raises_after_max_schema_corrections(tmp_path: Any) -> None:
    from agentruntime_worker.tools.catalog import ToolCatalog

    gateway = _FakeGateway([
        {"tool": "repo_read", "args": {}, "cost_micros": 500},
        {"tool": "repo_read", "args": {}, "cost_micros": 500},
        {"tool": "repo_read", "args": {}, "cost_micros": 500},
    ])
    builder = ContextBuilder(gateway, LocalDiskObjectStoreProvider(tmp_path / "obj", signing_key=b"k"), ContextConfig())  # type: ignore[arg-type]
    loop = ModelToolLoop(gateway, builder, tools=ToolCatalog(_FakeToolDriver()))  # type: ignore[arg-type]
    inp = ToolLoopStepInput(
        tenant=Tenant(tenant_id="t1", workspace_id="w1"),
        task_id="task-schema-2",
        round_seq=1,
        step_id="step-1",
        iteration=1,
        node={"id": "read", "tools": ["repo_read"]},
        history=[],
        task_state={"goal": "read readme"},
        task_plan={"goal": "read readme", "steps": []},
        task_family="general.coding",
    )
    with pytest.raises(InvalidError, match="bad_tool_arguments"):
        await loop.step(inp)
    assert len(gateway.calls) == 3
