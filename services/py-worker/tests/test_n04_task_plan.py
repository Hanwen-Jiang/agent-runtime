"""docs/plan/feedback-2026-11-01.md N-04：执行计划一开始就有，前端看得到，遮蔽之后不丢（人工验收基线 Q55、Q56）。

契约清单 tests/contract/fixtures/task_plan_contract_cases.json 的每一条在这里有对应：
steps_min_3、steps_max_8、step_shape、status_values、single_active、round_reference（workflows/plan.py 与 Planner 的检查），
event_full_snapshot（工作流推的 plan_updated），compaction_keeps_plan（Context Builder 的 brief），fast_no_plan，
old_client_unknown_fields（schema 只加了可选字段与新事件，见 tests/contract；旧事件的载荷不变，这里核对）。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from agentruntime_worker.context.builder import ContextBuilder
from agentruntime_worker.planner.planner import Planner
from agentruntime_worker.workflows import plan as plans

from .context_support import TASK_STATE, ContextGateway, history, objects, request as context_request, text_of
from .context_support import config as context_config
from .planner_support import PLAN_STEPS, FakeGateway, last_round, plan_answer, request, signals
from .support import assert_valid
from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .test_stage_runner import deliver, one_round, stage

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

RISKY_FULL = signals(effect="write_irreversible", explicit=True, confidence=0.6)


def steps(*statuses: str, round_: int = 1) -> list[dict[str, Any]]:
    return [{"id": f"s{i}", "title": f"第 {i} 步", "status": st, "round": round_} for i, st in enumerate(statuses, start=1)]


# --- 步骤的检查（workflows/plan.py，Planner 与 Task 工作流共用） ---


def test_three_to_eight_steps_are_accepted() -> None:
    assert plans.step_problems(steps("active", "pending", "pending"), 1) == []
    assert plans.step_problems(steps(*["pending"] * 8), 1) == []


@pytest.mark.parametrize("n", [0, 2, 9])
def test_fewer_than_three_or_more_than_eight_steps_are_rejected(n: int) -> None:
    found = plans.step_problems(steps(*["pending"] * n), 1)
    assert any("3～8" in p for p in found)


def test_two_active_steps_are_rejected() -> None:
    assert any("最多一步 active" in p for p in plans.step_problems(steps("active", "active", "pending"), 1))


def test_unknown_status_and_missing_fields_are_rejected() -> None:
    bad = steps("done", "pending", "pending")
    bad[0]["status"] = "skipped"
    del bad[1]["round"]
    bad[2]["title"] = " "
    found = plans.step_problems(bad, 1)
    assert any("status" in p for p in found) and any("round" in p for p in found) and any("title" in p for p in found)


def test_a_step_cannot_reference_a_later_round() -> None:
    assert any("晚于本轮" in p for p in plans.step_problems(steps("active", "pending", "pending", round_=3), 2))


def test_duplicate_ids_and_extra_fields_are_rejected() -> None:
    dup = steps("active", "pending", "pending")
    dup[1]["id"] = "s1"
    dup[2]["note"] = "x"
    found = plans.step_problems(dup, 1)
    assert any("重复" in p for p in found) and any("note" in p for p in found)


def test_steps_from_normalizes_and_snapshot_names_the_current_step() -> None:
    raw = {"steps": [{"id": "a", "title": "  读代码 ", "status": "done", "round": 1.0}, *steps("active", "pending")[0:2]]}
    raw["steps"][1]["id"], raw["steps"][2]["id"] = "b", "c"
    got = plans.steps_from(raw, 2)
    assert got is not None and got[0] == {"id": "a", "title": "读代码", "status": "done", "round": 1}
    snap = plans.snapshot(got, 2)
    assert snap["current_step"] == "b" and snap["round_seq"] == 2
    assert_valid("task_state", {**TASK_STATE, "task_id": "tsk_01J" + "0" * 22 + "C", "session_id": "ses_01J" + "0" * 22 + "C", "plan": snap})
    assert plans.steps_from({"steps": steps("active")}, 1) is None, "不合格的不用"
    assert plans.steps_from({"triage": {}}, 1) is None and plans.steps_from(None, 1) is None


def test_finishing_marks_the_active_step_done_or_blocked() -> None:
    plan = plans.snapshot(steps("done", "active", "pending"), 2)
    done = plans.finished(plan, 3, done=True)
    assert [s["status"] for s in done["steps"]] == ["done", "done", "pending"] and done["steps"][1]["round"] == 3
    assert "current_step" not in done
    blocked = plans.finished(plan, 3, done=False)
    assert [s["status"] for s in blocked["steps"]] == ["done", "blocked", "pending"]
    assert plans.finished(plans.snapshot(steps("done", "done", "done"), 2), 3, done=True) is None, "没有正在做的一步就不变"
    assert plans.finished(None, 1, done=True) is None


# --- Planner：第一次完整规划要给出整体步骤 ---


async def test_the_first_full_plan_must_carry_steps() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer(steps=None), plan_answer()])
    result = await Planner(gateway).plan(request(round_seq=1))
    assert not result.fallback and len(gateway.calls("plan")) == 2, "没给 steps 时重问一次"
    assert result.task_plan["steps"] == PLAN_STEPS
    retry = gateway.calls("plan")[1].generation.messages[-1].text
    assert "task_plan.steps" in retry
    assert "3～8 步" in gateway.calls("plan")[0].generation.messages[0].text, "规则写在 system 区"


async def test_invalid_steps_are_sent_back() -> None:
    two_active = [dict(PLAN_STEPS[0]), {**PLAN_STEPS[1], "status": "active"}, dict(PLAN_STEPS[2])]
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer(steps=two_active), plan_answer()])
    result = await Planner(gateway).plan(request(round_seq=1))
    assert not result.fallback and len(gateway.calls("plan")) == 2


async def test_later_rounds_may_keep_the_plan_without_restating_it() -> None:
    state = {"goal": "g", "done_when": "d", "plan": plans.snapshot(PLAN_STEPS, 1)}
    verdict = {"verdict": "replan", "reason": "数据不全", "replan": {"keep_nodes": [], "reason": "数据不全"}}
    gateway = FakeGateway(plan=[plan_answer(level="replan", steps=None)])
    result = await Planner(gateway).plan(request(round_seq=2, task_state=state, last_round=last_round(verdict)))
    assert not result.fallback and "steps" not in result.task_plan


async def test_fast_gives_no_plan() -> None:
    gateway = FakeGateway(triage=signals(kind="chitchat", family=None, effect="none"))
    result = await Planner(gateway).plan(request("你好"))
    assert result.triage_level == "fast" and result.task_plan is None


# --- Context Builder：计划在 brief 区，压缩之后还在（compaction_keeps_plan） ---


async def test_the_plan_stays_in_the_brief_through_compaction(tmp_path: Path) -> None:
    gateway = ContextGateway()
    builder = ContextBuilder(gateway, objects(tmp_path), context_config(effective_tokens=4_000))
    plan = plans.snapshot([{"id": "locate", "title": "定位出错的测试", "status": "done", "round": 1},
                           {"id": "fix_code", "title": "修改结账模块", "status": "active", "round": 2},
                           {"id": "verify", "title": "跑全部测试确认", "status": "pending", "round": 1}], 2)
    req = dataclasses.replace(context_request(history(40, chars=600)), task_state={**TASK_STATE, "plan": plan})
    built = await builder.build(req)
    assert "mask" in built.applied, "这个上下文要压缩"
    text = text_of(built.messages)
    assert "[正在做] 修改结账模块（fix_code）" in text and "[已完成] 定位出错的测试" in text and "[待做] 跑全部测试确认" in text
    again = await builder.build(dataclasses.replace(req, history=history(41, chars=600)))
    brief = [m.text for m in again.messages if "整体计划" in m.text]
    assert brief and brief == [m.text for m in built.messages if "整体计划" in m.text], "同一轮里每一步的 brief 相同"


# --- Task 工作流：Task State 的 plan 与 plan_updated（event_full_snapshot） ---


def _plan(*statuses: str, round_: int = 1) -> dict[str, Any]:
    return {"triage": {"level": "full"}, "horizon": "per_stage", "steps": steps(*statuses, round_=round_)}


def _events(h: StageHarness, task_id: str) -> list[dict[str, Any]]:
    return h.script.progress(task_id)


async def test_the_plan_is_in_task_state_and_each_change_is_published(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, deliver("answer")), stage(2, deliver("answer")), stage(3, deliver("answer"))]
    s.deliver["main"] = "answer"
    s.task_plans = {1: _plan("active", "pending", "pending"), 2: _plan("active", "pending", "pending"), 3: _plan("done", "active", "pending", round_=3)}
    inp, result = await harness.run()
    events = _events(harness, inp.task_id)
    names = [e["event"] for e in events]
    plan_events = [e for e in events if e["event"] == "plan_updated"]
    assert len(plan_events) == 3, "第 1 轮、第 3 轮变了，结束时一份；第 2 轮没变不推"
    first = names.index("plan_updated")
    assert names[first + 1] == "stage_started", "计划在 stage_started 之前"
    assert plan_events[0]["round_seq"] == 1 and plan_events[0]["current_step"] == "s1" and len(plan_events[0]["steps"]) == 3
    assert plan_events[1]["round_seq"] == 3 and plan_events[1]["current_step"] == "s2"
    final = plan_events[2]
    assert final["round_seq"] == 3 and [x["status"] for x in final["steps"]] == ["done", "done", "pending"] and "current_step" not in final
    assert names[-1] == "plan_updated", "最后一份在收尾时推，之后只有 Session 推的 task_finished"
    for e in plan_events:
        assert_valid("task_event_payload", e)
    state = next(f for f in s.finalized if f.task_id == inp.task_id).task_state
    assert state["plan"]["steps"] == final["steps"] and state["plan"]["round_seq"] == 3
    keys = [k for k, e, _ in s.published if e.task_id == inp.task_id and "plan_updated" in k]
    assert keys == ["r1/plan_updated", "r3/plan_updated", "r3/plan_updated/final"]
    assert s.plan_inputs[1].task_state["plan"]["current_step"] == "s1", "下一轮的 Planner 看得到计划"


async def test_an_aborted_task_marks_the_current_step_blocked(harness: StageHarness) -> None:
    s = harness.script
    one_round(harness, deliver("answer"))
    s.task_plans = {1: _plan("done", "active", "pending")}
    s.reviewer = _verdicts("abort")
    inp, _ = await harness.run()
    final = [e for e in _events(harness, inp.task_id) if e["event"] == "plan_updated"][-1]
    assert [x["status"] for x in final["steps"]] == ["done", "blocked", "pending"]


async def test_fast_and_plans_without_steps_publish_nothing(harness: StageHarness) -> None:
    s = harness.script
    one_round(harness, deliver("answer"))
    s.triage_level = "fast"
    inp, _ = await harness.run()
    assert not [e for e in _events(harness, inp.task_id) if e["event"] == "plan_updated"]
    state = next(f for f in s.finalized if f.task_id == inp.task_id).task_state
    assert "plan" not in state


async def test_invalid_steps_from_a_later_round_keep_the_current_plan(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, deliver("answer")), stage(2, deliver("answer"))]
    s.deliver["main"] = "answer"
    s.task_plans = {1: _plan("active", "pending", "pending"), 2: _plan("active", "active", "pending", round_=2)}
    inp, _ = await harness.run()
    plan_events = [e for e in _events(harness, inp.task_id) if e["event"] == "plan_updated"]
    assert [e["round_seq"] for e in plan_events] == [1, 2], "第 2 轮的两步 active 不用；结束时把第 1 轮的那份收尾"
    assert plan_events[-1]["steps"][0]["status"] == "done" and plan_events[-1]["steps"][1]["status"] == "pending"


async def test_old_events_are_unchanged(harness: StageHarness) -> None:
    """old_client_unknown_fields：旧客户端照常消费原有事件；plan_updated 之外的载荷不多出字段。"""
    s = harness.script
    one_round(harness, deliver("answer"))
    s.task_plan = _plan("active", "pending", "pending")
    inp, _ = await harness.run()
    for e in _events(harness, inp.task_id):
        assert_valid("task_event_payload", e)
        if e["event"] != "plan_updated":
            assert "steps" not in e and "current_step" not in e


def _verdicts(*kinds: str):
    """按 Round 依次给出判定：done 交付 answer。"""

    async def review(inp: Any) -> dict[str, Any]:
        kind = kinds[min(inp.round_seq, len(kinds)) - 1]
        if kind == "done":
            return {"verdict": "done", "reason": "完成", "done": {"deliver_node": "answer"}}
        if kind == "abort":
            return {"verdict": "abort", "reason": "做不下去", "abort": {"completed": [], "not_completed": ["全部"]}}
        return {"verdict": "continue", "reason": "接着做"}

    return review
