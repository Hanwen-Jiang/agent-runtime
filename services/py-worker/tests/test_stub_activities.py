"""S-03 验收：桩 activity（计划 S-03 目标：Planner 与 Reviewer 先用桩，固定计划、永远判 done）。

桩的输出必须合乎 /schemas，后续 S-04、S-09、S-10 换成真实实现时契约不变；长 activity 要发心跳，
取消能传进来（计划 S-03 难点“心跳”“取消”）；调用记录是端到端测试断言“不重跑”的依据。
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from agentruntime_worker import names
from agentruntime_worker.activities.calllog import read_calls
from agentruntime_worker.activities.stubs import HUMAN_OPTIONS, STUB_PLANS, StubActivities
from agentruntime_worker.activities.types import (
    FinalizeTaskInput,
    NodeInput,
    PlanRoundInput,
    ReviewRoundInput,
    StageResult,
    Tenant,
)
from agentruntime_worker.config import WorkerConfig

from .support import SESSION_ID, assert_valid, new_task_id

TENANT = Tenant(tenant_id="t1", user_id="u1", session_id=SESSION_ID)
TRIAGE_LEVELS = {"fast", "light", "full", "replan"}


@pytest.fixture
def config(tmp_path: Path) -> WorkerConfig:
    (tmp_path / "gates").mkdir()
    return WorkerConfig(call_log=tmp_path / "calls.jsonl", gate_dir=tmp_path / "gates", stub_node_timeout_s=40)


@pytest.fixture
def stubs(config: WorkerConfig) -> StubActivities:
    return StubActivities(config)


def activity_env(name: str, queue: str, *, attempt: int = 1, heartbeat: timedelta | None = None) -> ActivityEnvironment:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info, activity_type=name, task_queue=queue, attempt=attempt, heartbeat_timeout=heartbeat
    )
    return env


def plan_input(task_id: str, stub_plan: str, round_seq: int = 1, stub_rounds: int = 1) -> PlanRoundInput:
    return PlanRoundInput(
        tenant=TENANT,
        task_id=task_id,
        round_seq=round_seq,
        task_state={"task_id": task_id, "session_id": SESSION_ID, "goal": "g", "done_when": "d"},
        message="上季度退款率为什么上升？",
        inputs={"stub_plan": stub_plan, "stub_rounds": stub_rounds},
        attachments=[],
        pinned={"recipe": "stub@v1", "node_templates": {"stub_answer": "v2"}},
    )


async def plan(stubs: StubActivities, task_id: str, stub_plan: str, **kw: Any) -> dict[str, Any]:
    out = await activity_env(names.PLAN_ROUND, names.DECISION_QUEUE).run(stubs.plan_round, plan_input(task_id, stub_plan, **kw))
    assert out.triage_level in TRIAGE_LEVELS
    return out.stage


def node(stage: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(n for n in stage["nodes"] if n["id"] == node_id)


# --- Planner 桩 ---


@pytest.mark.parametrize("stub_plan", STUB_PLANS)
async def test_stub_plans_are_valid_stages(stubs: StubActivities, config: WorkerConfig, stub_plan: str) -> None:
    stage = await plan(stubs, new_task_id(), stub_plan)
    assert_valid("stage", stage)
    ids = [n["id"] for n in stage["nodes"]]
    assert len(ids) == len(set(ids)), "Node id 不能重复"
    delivers = [n for n in stage["nodes"] if n.get("role") == "deliver"]
    assert len(delivers) == 1, "最终交付只来自一个 role = deliver 的节点（附录 B.9）"
    for n in stage["nodes"]:
        if n.get("executor") == "model":
            assert n["budget"]["timeout_s"] == config.stub_node_timeout_s
        for dep in n.get("after", []):
            assert dep in ids, f"{n['id']} 依赖的 {dep} 不在 Stage 里"


async def test_human_gate_plan_waits_for_confirmation(stubs: StubActivities) -> None:
    stage = await plan(stubs, new_task_id(), "human_gate")
    confirm = node(stage, "confirm")
    assert confirm["executor"] == "human" and confirm["role"] == "gate"
    assert tuple(confirm["inputs"]["options"]) == HUMAN_OPTIONS
    assert "confirm" in node(stage, "answer").get("after", [])


async def test_unknown_stub_plan_is_rejected(stubs: StubActivities) -> None:
    with pytest.raises(ApplicationError) as err:
        await plan(stubs, new_task_id(), "no_such_plan")
    assert err.value.non_retryable


async def test_stub_rounds_are_carried_into_the_model_node(stubs: StubActivities) -> None:
    first = await plan(stubs, new_task_id(), "answer", round_seq=1, stub_rounds=2)
    last = await plan(stubs, new_task_id(), "answer", round_seq=2, stub_rounds=2)
    assert node(first, "answer")["inputs"]["final"] is False
    assert node(last, "answer")["inputs"]["final"] is True


# --- 桩模型 ---


async def model_output(stubs: StubActivities, task_id: str, stage: dict[str, Any], **env_kw: Any) -> dict[str, Any]:
    env = activity_env(names.EXECUTE_MODEL_NODE, names.MODEL_QUEUE, **env_kw)
    return await env.run(stubs.execute_model_node, NodeInput(tenant=TENANT, task_id=task_id, round_seq=1, node=node(stage, "answer")))


async def test_stub_model_output_is_a_valid_node_output(stubs: StubActivities) -> None:
    task_id = new_task_id()
    out = await model_output(stubs, task_id, await plan(stubs, task_id, "answer"))
    assert_valid("node_output", out)
    assert out["result"]["text"]
    assert out["result"]["final"] is True
    assert "rationale" in out, "桩模型也给出 rationale，用来检验工作流不把它传给下游"


async def test_slow_model_heartbeats_until_the_gate_opens(stubs: StubActivities, config: WorkerConfig) -> None:
    task_id = new_task_id()
    stage = await plan(stubs, task_id, "slow_answer")
    env = activity_env(names.EXECUTE_MODEL_NODE, names.MODEL_QUEUE, heartbeat=timedelta(milliseconds=300))
    beats: list[Any] = []
    env.on_heartbeat = lambda *details: beats.append(details)
    run = asyncio.create_task(env.run(stubs.execute_model_node, NodeInput(tenant=TENANT, task_id=task_id, round_seq=1, node=node(stage, "answer"))))
    await asyncio.sleep(0.6)
    assert not run.done(), "门没开之前不应返回"
    assert beats, "等待期间要发心跳"
    assert config.gate_dir is not None
    (config.gate_dir / task_id).touch()
    out = await asyncio.wait_for(run, timeout=5)
    assert_valid("node_output", out)


async def test_slow_model_stops_when_cancelled(stubs: StubActivities, config: WorkerConfig) -> None:
    task_id = new_task_id()
    stage = await plan(stubs, task_id, "slow_answer")
    env = activity_env(names.EXECUTE_MODEL_NODE, names.MODEL_QUEUE, heartbeat=timedelta(milliseconds=300))
    run = asyncio.create_task(env.run(stubs.execute_model_node, NodeInput(tenant=TENANT, task_id=task_id, round_seq=1, node=node(stage, "answer"))))
    await asyncio.sleep(0.3)
    env.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, timeout=5)
    assert config.call_log is not None
    events = [c["event"] for c in read_calls(config.call_log) if c["activity"] == names.EXECUTE_MODEL_NODE and c["task_id"] == task_id]
    assert "cancelled" in events and "end" not in events


async def test_failing_model_raises_a_non_retryable_error(stubs: StubActivities) -> None:
    task_id = new_task_id()
    stage = await plan(stubs, task_id, "fail_model")
    with pytest.raises(ApplicationError) as err:
        await model_output(stubs, task_id, stage)
    assert err.value.non_retryable


# --- Reviewer 桩 ---


async def review(stubs: StubActivities, results: dict[str, StageResult]) -> dict[str, Any]:
    env = activity_env(names.REVIEW_ROUND, names.DECISION_QUEUE)
    task_id = new_task_id()
    return await env.run(
        stubs.review_round,
        ReviewRoundInput(
            tenant=TENANT,
            task_id=task_id,
            round_seq=1,
            task_state={"task_id": task_id, "session_id": SESSION_ID, "goal": "g", "done_when": "d"},
            stage_results=results,
            done_when="d",
        ),
    )


async def test_reviewer_judges_done_with_the_deliver_node(stubs: StubActivities) -> None:
    verdict = await review(
        stubs,
        {
            "confirm": StageResult(role="gate", status="succeeded", result={"option": "approve"}),
            "answer": StageResult(role="deliver", status="succeeded", result={"text": "x", "final": True}),
        },
    )
    assert_valid("verdict", verdict)
    assert verdict["verdict"] == "done"
    assert verdict["done"]["deliver_node"] == "answer"


async def test_reviewer_continues_while_results_are_not_final(stubs: StubActivities) -> None:
    verdict = await review(stubs, {"answer": StageResult(role="deliver", status="succeeded", result={"text": "x", "final": False})})
    assert_valid("verdict", verdict)
    assert verdict["verdict"] == "continue"


# --- 收尾与调用记录 ---


async def test_call_log_records_each_activity_with_attempt_and_queue(stubs: StubActivities, config: WorkerConfig) -> None:
    task_id = new_task_id()
    await activity_env(names.PLAN_ROUND, names.DECISION_QUEUE, attempt=2).run(stubs.plan_round, plan_input(task_id, "answer"))
    await activity_env(names.FINALIZE_TASK, names.DECISION_QUEUE).run(
        stubs.finalize_task,
        FinalizeTaskInput(tenant=TENANT, task_id=task_id, status="done", task_state={}, deliver_node="answer"),
    )
    assert config.call_log is not None
    calls = [c for c in read_calls(config.call_log) if c["task_id"] == task_id]
    assert [(c["activity"], c["event"]) for c in calls] == [
        (names.PLAN_ROUND, "start"),
        (names.PLAN_ROUND, "end"),
        (names.FINALIZE_TASK, "start"),
        (names.FINALIZE_TASK, "end"),
    ]
    assert calls[0]["attempt"] == 2
    assert all(c["task_queue"] == names.DECISION_QUEUE for c in calls)
    assert all(isinstance(c["pid"], int) and c["ts"] for c in calls)


def test_read_calls_of_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_calls(tmp_path / "missing.jsonl") == []
