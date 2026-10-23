"""T-15：Worker 的 span 按附录 J.1 与 GenAI 语义约定命名（细化见 docs/plan/deploy-telemetry.md 第 2 节第 2 条）。

- Node Executor：model 节点一个 `chat` span，program 节点一个 `execute_tool {工具}` span，都带 Temporal 的 WorkflowID、RunID。
- 收尾写回：事后补记 Task 级的 `invoke_workflow` 与每个 Round 的 `runtime.round`（子 span），供 Learning Pipeline 消费。
- Stage 的形状（stage_summary 的 shape）只有结构字段，不带 inputs、rationale 与结果。
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from google.protobuf import json_format, struct_pb2
from temporalio.testing import ActivityEnvironment

from agentruntime.common.v1 import metering_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker import names
from agentruntime_worker.activities.types import FinalizeTaskInput
from agentruntime_worker.reviewer.wrap_up import RecordingMemoryClient, WrapUp
from agentruntime_worker.workflows import verdicts as vd

from .executor_harness import TASK_ID, TENANT, ExecutorFixture, make_fixture, model, node_input, program

RUN_ID = "0193f4c2-0000-7000-8000-00000000run1"
USER_TEXT = "华东区 2026 年 9 月的退款明细（用户原文）"


@pytest.fixture
def fx(tmp_path: Path) -> ExecutorFixture:
    return make_fixture(tmp_path)


def env_for(name: str, queue: str) -> ActivityEnvironment:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info,
        activity_type=name,
        task_queue=queue,
        workflow_id=TASK_ID,
        workflow_run_id=RUN_ID,
        heartbeat_timeout=timedelta(seconds=2),
        start_to_close_timeout=timedelta(seconds=30),
    )
    return env


def exported(telemetry: ConsoleTelemetryProvider) -> str:
    records = [*telemetry.exported_spans, *telemetry.exported_metrics, *telemetry.exported_logs]
    return json.dumps([dataclasses.asdict(r) for r in records], ensure_ascii=False, default=str)


def spans(telemetry: ConsoleTelemetryProvider, name: str) -> list[Any]:
    return [s for s in telemetry.exported_spans if s.name == name]


def scripted_reply(fx: ExecutorFixture, result: dict[str, Any], *, rationale: str = "", usage: metering_pb2.Usage | None = None) -> None:
    value = struct_pb2.Value()
    json_format.ParseDict(result, value)
    fx.models.responses.append(
        mg.GenerateResponse(
            result=value,
            rationale=rationale,
            finish_reason=mg.FINISH_REASON_STOP,
            model=mg.ModelChoice(model_profile_id="test/strong-a@1", tier="strong@medium", vendor="anthropic"),
            usage=usage or metering_pb2.Usage(),
        )
    )


# --- Node Executor ---


async def test_model_node_records_a_chat_span(fx: ExecutorFixture) -> None:
    scripted_reply(fx, {"summary": "x"}, usage=metering_pb2.Usage(input_tokens=1200, output_tokens=300, cost_usd_micros=4500))
    node = model("analyze", tier="strong@medium", reasoning="stepwise", quality={"type": "none"})
    await env_for(names.EXECUTE_MODEL_NODE, names.MODEL_QUEUE).run(fx.executor.execute_model_node, node_input(node))
    [span] = spans(fx.telemetry, "chat")
    a = span.attributes
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.response.model"] == "test/strong-a@1"
    assert a["gen_ai.usage.input_tokens"] == 1200 and a["gen_ai.usage.output_tokens"] == 300
    assert a["runtime.cost_usd"] == pytest.approx(0.0045)
    assert (a["role"], a["template"], a["reasoning"], a["tier"]) == ("transform", "summarize@v2", "stepwise", "strong@medium")
    assert a["quality"] == "none"
    assert (a["task_id"], a["node_id"]) == (TASK_ID, "analyze")
    assert (a["temporal.workflow_id"], a["temporal.run_id"]) == (TASK_ID, RUN_ID)
    assert a["tenant_id"] == TENANT.tenant_id
    assert not spans(fx.telemetry, "node.execute"), "旧名字 node.execute 不再使用"


async def test_program_node_records_an_execute_tool_span(fx: ExecutorFixture) -> None:
    node = program("crm1", "update_crm", effect="write_reversible", inputs={"customer": USER_TEXT})
    await env_for(names.EXECUTE_PROGRAM_NODE, names.PROGRAM_QUEUE).run(fx.executor.execute_program_node, node_input(node))
    [span] = spans(fx.telemetry, "execute_tool update_crm")
    a = span.attributes
    assert a["gen_ai.operation.name"] == "execute_tool"
    assert a["gen_ai.tool.name"] == "update_crm"
    assert a["effect"] == "write_reversible"
    assert a["idempotency_key"] == node["idempotency_key"]
    assert (a["temporal.workflow_id"], a["temporal.run_id"]) == (TASK_ID, RUN_ID)


async def test_inputs_and_rationale_never_reach_the_trace_payloads(fx: ExecutorFixture) -> None:
    """rationale 只写入 trace 的事件（S-05 已有的规则），但 Node 的 inputs 与模型的结果不进 span 属性。"""
    scripted_reply(fx, {"summary": "RESULT-TEXT"}, rationale="")
    await env_for(names.EXECUTE_MODEL_NODE, names.MODEL_QUEUE).run(fx.executor.execute_model_node, node_input(model("m1", inputs={"text": USER_TEXT})))
    await env_for(names.EXECUTE_PROGRAM_NODE, names.PROGRAM_QUEUE).run(
        fx.executor.execute_program_node, node_input(program("p1", "lookup_rate", inputs={"region": USER_TEXT}))
    )
    text = exported(fx.telemetry)
    assert USER_TEXT not in text
    assert "RESULT-TEXT" not in text


async def test_failed_nodes_mark_their_span_as_error(fx: ExecutorFixture) -> None:
    async def boom(_: Any) -> Any:
        raise RuntimeError("tool exploded")

    fx.behaviors["lookup_rate"] = boom
    with pytest.raises(Exception):
        await env_for(names.EXECUTE_PROGRAM_NODE, names.PROGRAM_QUEUE).run(fx.executor.execute_program_node, node_input(program("p1", "lookup_rate")))
    [span] = spans(fx.telemetry, "execute_tool lookup_rate")
    assert span.status == "error"


# --- Stage 的形状 ---


STAGE = {
    "seq": 2,
    "goal": "按客户归类发票",
    "exit_when": "每张发票都有科目",
    "rationale": "PLANNER-RATIONALE",
    "nodes": [
        {
            "id": "fetch",
            "kind": "atomic",
            "template": "invoice_fetch@v1",
            "executor": "program",
            "effect": "read",
            "role": "fetch",
            "inputs": {"month": USER_TEXT},
            "rationale": "NODE-RATIONALE",
        },
        {
            "id": "code",
            "kind": "atomic",
            "template": "invoice_code@v3",
            "executor": "model",
            "output": "structured",
            "effect": "none",
            "role": "transform",
            "after": ["fetch"],
            "tier": "mid@low",
            "reasoning": "direct",
            "quality": {"type": "none"},
            "inputs": {"rows": {"$ref": "fetch"}},
        },
        {
            "id": "post",
            "kind": "atomic",
            "template": "ledger_post@v2",
            "executor": "program",
            "effect": "write_reversible",
            "role": "act",
            "after": ["code"],
            "idempotency_key": "ik-xyz",
            "inputs": {"rows": {"$ref": "code"}},
        },
        {
            "id": "check",
            "kind": "goal",
            "goal": "核对过账结果",
            "done_when": "总额一致",
            "mode": "tool_loop",
            "output_schema": {"type": "object"},
            "after": ["post"],
        },
    ],
}


def test_stage_shape_keeps_structure_only() -> None:
    summary = vd.stage_summary(STAGE)
    assert (summary["seq"], summary["goal"], summary["nodes"], summary["tiers"]) == (2, "按客户归类发票", 4, ["mid@low"]), "原有的摘要字段不变"
    shape = summary["shape"]
    assert shape["goal"] == "按客户归类发票" and shape["exit_when"] == "每张发票都有科目"
    nodes = {n["id"]: n for n in shape["nodes"]}
    assert nodes["code"] == {
        "id": "code",
        "kind": "atomic",
        "template": "invoice_code@v3",
        "after": ["fetch"],
        "tier": "mid@low",
        "reasoning": "direct",
        "quality": {"type": "none"},
    }
    assert nodes["check"] == {
        "id": "check",
        "kind": "goal",
        "goal": "核对过账结果",
        "done_when": "总额一致",
        "mode": "tool_loop",
        "output_schema": {"type": "object"},
        "after": ["post"],
    }
    text = json.dumps(shape, ensure_ascii=False)
    for leaked in (USER_TEXT, "PLANNER-RATIONALE", "NODE-RATIONALE", "ik-xyz", "inputs", "executor", "effect", '"role"'):
        assert leaked not in text, f"形状里不能有 {leaked}"


# --- 收尾写回：Task 与 Round 两级 span ---


@pytest.fixture
def objects(tmp_path: Path) -> LocalDiskObjectStoreProvider:
    return LocalDiskObjectStoreProvider(tmp_path / "objects", signing_key=os.urandom(32))


def finalize_input(status: str = "done") -> FinalizeTaskInput:
    shape1 = vd.stage_summary({**STAGE, "seq": 1})
    shape2 = vd.stage_summary({**STAGE, "seq": 2, "goal": "补齐缺科目的发票"})
    rounds = [
        {"seq": 1, "stage_goal": "按客户归类发票", "triage_level": "full", "status": "completed", "verdict": "continue", "cost_usd": 0.12},
        {"seq": 2, "stage_goal": "补齐缺科目的发票", "status": "completed", "verdict": "done" if status == "done" else "abort", "cost_usd": 0.05},
    ]
    return FinalizeTaskInput(
        tenant=TENANT,
        task_id=TASK_ID,
        status=status,
        task_state={"rounds": rounds, "pinned": {"recipe": ""}, "goal": USER_TEXT, "done_when": "每张发票都有科目"},
        verdict={"verdict": "done", "reason": "完成", "done": {"deliver_node": "check"}} if status == "done" else None,
        deliver_node="check" if status == "done" else "",
        stages=[shape1, shape2],
        task_family="finance.invoice_coding",
    )


async def run_finalize(objects: LocalDiskObjectStoreProvider, telemetry: ConsoleTelemetryProvider, inp: FinalizeTaskInput) -> None:
    wrap = WrapUp(objects=objects, memory=RecordingMemoryClient(), telemetry=telemetry)
    await env_for(names.FINALIZE_TASK, names.DECISION_QUEUE).run(wrap.finalize, inp)


async def test_wrap_up_records_the_task_span(objects: LocalDiskObjectStoreProvider) -> None:
    telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
    await run_finalize(objects, telemetry, finalize_input())
    [task] = spans(telemetry, "invoke_workflow")
    a = task.attributes
    assert (a["tenant_id"], a["session_id"], a["task_id"]) == (TENANT.tenant_id, TENANT.session_id, TASK_ID)
    assert (a["temporal.workflow_id"], a["temporal.run_id"]) == (TASK_ID, RUN_ID)
    assert a["gen_ai.operation.name"] == "invoke_workflow"
    assert a["recipe"] == ""
    assert a["runtime.task_family"] == "finance.invoice_coding"
    assert a["runtime.task_status"] == "done"
    assert a["runtime.cost_usd"] == pytest.approx(0.17)
    assert a["runtime.rounds"] == 2
    assert a["runtime.synthetic"] is True, "事后补记的 span 标出来，时间戳不是 Task 的真实起止"


async def test_wrap_up_records_one_round_span_per_round(objects: LocalDiskObjectStoreProvider) -> None:
    telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
    await run_finalize(objects, telemetry, finalize_input())
    [task] = spans(telemetry, "invoke_workflow")
    rounds = sorted(spans(telemetry, "runtime.round"), key=lambda s: s.attributes["round_seq"])
    assert [r.attributes["round_seq"] for r in rounds] == [1, 2]
    for r in rounds:
        assert r.parent_span_id == task.span_id and r.trace_id == task.trace_id
        assert r.attributes["runtime.synthetic"] is True
    assert [r.attributes["verdict"] for r in rounds] == ["continue", "done"]
    assert rounds[0].attributes["triage_level"] == "full"
    assert "triage_level" not in rounds[1].attributes, "原地重跑的 Round 没有经过 Planner"
    assert [r.attributes["runtime.cost_usd"] for r in rounds] == pytest.approx([0.12, 0.05])
    shape = json.loads(rounds[1].attributes["runtime.stage_shape"])
    assert shape["goal"] == "补齐缺科目的发票"
    assert [n["id"] for n in shape["nodes"]] == ["fetch", "code", "post", "check"]


async def test_round_spans_carry_no_user_data(objects: LocalDiskObjectStoreProvider) -> None:
    telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
    await run_finalize(objects, telemetry, finalize_input())
    text = exported(telemetry)
    for leaked in (USER_TEXT, "PLANNER-RATIONALE", "NODE-RATIONALE", "ik-xyz"):
        assert leaked not in text


@pytest.mark.parametrize("status", ["aborted", "cancelled", "failed"])
async def test_every_outcome_is_traced(objects: LocalDiskObjectStoreProvider, status: str) -> None:
    """Learning 算成功率要分母：没做成的 Task 也要有 Task 级 span。"""
    telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
    await run_finalize(objects, telemetry, finalize_input(status))
    [task] = spans(telemetry, "invoke_workflow")
    assert task.attributes["runtime.task_status"] == status


async def test_task_spans_are_bound_to_the_task_tenant(objects: LocalDiskObjectStoreProvider) -> None:
    telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
    await run_finalize(objects, telemetry, finalize_input())
    assert {s.attributes["tenant_id"] for s in telemetry.exported_spans} == {TENANT.tenant_id}
    assert await objects.bind(TenantContext(tenant_id=TENANT.tenant_id)).head(f"tasks/{TASK_ID}/task_state.json"), "原有的归档不受影响"
