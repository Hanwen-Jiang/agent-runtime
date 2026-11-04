"""docs/plan/feedback-2026-11-01.md N-01：嵌在文字里的引用、引用之前各轮的结果、deliver 没写引用时带上上游。

线上日志（2026-11-01）：第 8 轮的交付原样写着 “$diagnose_test_failure.result.root_cause”，模型回答“输入变量没有解析”；
第 4 轮只有一个 deliver，引用不到第 3 轮的 run_tests，交付是空的。
"""

from __future__ import annotations

import pytest

from agentruntime.common.v1 import enums_pb2

from agentruntime_worker.planner import validate
from agentruntime_worker.planner.context import CatalogView, PlanningContext
from agentruntime_worker.workflows import stage_plan as sp
from agentruntime_worker.workflows.patches import AT_START, INTERRUPT_WITHDRAW_PATCH, REFERENCES_PATCH

from .stage_harness import StageHarness, harness, returns  # noqa: F401 — harness 是 fixture
from .test_stage_runner import atomic, deliver, stage

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

RESULTS = {"diagnose": {"root_cause": "缺少 psycopg", "test_command": "pytest -q"}, "fix": {"files": ["a.py", "b.py"]}}


# --- 纯函数：嵌在文字里的引用 ---


def test_references_embedded_in_text_are_replaced() -> None:
    text = "根因：$diagnose.result.root_cause；命令：$diagnose.result.test_command。改了 $fix.result.files"
    out = sp.resolve_inputs({"summary": text}, results=RESULTS, task_state={}, embedded=True)
    assert out == {"summary": '根因：缺少 psycopg；命令：pytest -q。改了 ["a.py","b.py"]'}, "字符串原样嵌入，其余写成紧凑 JSON；句末的“。”不算路径"


def test_a_whole_string_reference_keeps_its_type() -> None:
    out = sp.resolve_inputs({"files": "$fix.result.files", "all": "$diagnose.result"}, results=RESULTS, task_state={}, embedded=True)
    assert out == {"files": ["a.py", "b.py"], "all": RESULTS["diagnose"]}


def test_a_missing_path_embeds_the_whole_result() -> None:
    out = sp.interpolate("见 $diagnose.result.no_such_field", results=RESULTS, task_state={})
    assert "缺少 psycopg" in out and "$" not in out, "路径不存在而 Node 的结果在：嵌入整个结果，不留占位符"


def test_a_missing_node_fails_instead_of_passing_the_placeholder() -> None:
    with pytest.raises(sp.UnresolvedReference):
        sp.resolve_inputs({"summary": "根因：$gone.result.root_cause"}, results=RESULTS, task_state={}, embedded=True)


def test_dollar_signs_that_are_not_references_stay() -> None:
    out = sp.resolve_inputs({"a": "价格 $5，$$diagnose.result 是字面的", "b": "$5 per unit"}, results=RESULTS, task_state={}, embedded=True)
    assert out == {"a": "价格 $5，$diagnose.result 是字面的", "b": "$5 per unit"}


def test_task_state_references_embed_too() -> None:
    out = sp.interpolate("目标：$task.goal", results={}, task_state={"goal": "跑测试"})
    assert out == "目标：跑测试"


def test_long_values_are_clipped() -> None:
    big = {"log": "x" * (sp.EMBEDDED_VALUE_CHARS + 500)}
    out = sp.interpolate("日志：$big.result", results={"big": big}, task_state={})
    assert len(out) < sp.EMBEDDED_VALUE_CHARS + 100 and "截断" in out


def test_old_runs_keep_the_old_rules() -> None:
    """之前的运行（embedded 为 False）：嵌在文字里的原样，与改动之前一样。"""
    text = "根因：$diagnose.result.root_cause"
    assert sp.resolve_inputs({"s": text}, results=RESULTS, task_state={}) == {"s": text}


# --- 纯函数：校验 ---


def _stage(*nodes: dict) -> dict:
    return stage(2, *nodes)


def test_validation_allows_results_from_earlier_rounds() -> None:
    s = _stage(deliver(inputs={"summary": "根因：$diagnose.result.root_cause", "files": "$fix.result.files"}))
    assert sp.validate_stage(s, max_nodes=10) != [], "之前的运行：不是祖先的引用不合法"
    assert sp.validate_stage(s, max_nodes=10, earlier={"diagnose", "fix"}) == []


def test_validation_rejects_unknown_ids_and_lists_what_is_available() -> None:
    s = _stage(deliver(inputs={"summary": "根因：$guess.result.root_cause"}))
    (error,) = sp.validate_stage(s, max_nodes=10, earlier={"diagnose"})
    assert "guess" in error and "diagnose" in error, "错误说明要列出可以引用的之前的结果，Planner 重问时据此改"


def test_an_id_in_this_stage_must_still_be_an_ancestor() -> None:
    """本 Stage 里有同名的 Node：只能是祖先，不能因为之前有过同名的结果就绕过 after。"""
    s = _stage(atomic("diagnose"), deliver(inputs={"summary": "$diagnose.result"}))
    assert sp.validate_stage(s, max_nodes=10, earlier={"diagnose"}) != []
    s = _stage(atomic("diagnose"), deliver(after=["diagnose"], inputs={"summary": "$diagnose.result"}))
    assert sp.validate_stage(s, max_nodes=10, earlier={"diagnose"}) == []


def test_planner_validation_uses_the_same_rule() -> None:
    context = PlanningContext(catalog=CatalogView(templates={"answer@v1": ("model", "none")}), effect_ceiling="read", risk_authorized="read")
    s = {"seq": 2, "goal": "交付", "exit_when": "交付了", "nodes": [{"id": "answer", "kind": "atomic", "template": "answer@v1", "executor": "model", "output": "generate", "effect": "none", "role": "deliver", "inputs": {"s": "$diagnose.result"}}]}
    plan = {"triage": {"level": "full"}, "horizon": "per_stage"}
    assert any("diagnose" in p for p in validate.problems(s, plan, level="full", context=context, keep_nodes=[]))
    assert not any("diagnose" in p for p in validate.problems(s, plan, level="full", context=context, keep_nodes=[], earlier={"diagnose": "{…}"}))


# --- 纯函数：deliver 的输入 ---


def test_deliver_without_references_gets_its_upstream() -> None:
    node = deliver(after=["run_tests"])
    extra = sp.deliver_inputs(node, results={"run_tests": {"failed": 24}}, earlier={}, user_message="跑测试并修好")
    assert extra == {"upstream": {"run_tests": {"failed": 24}}, "user_message": "跑测试并修好"}


def test_deliver_with_no_after_gets_the_earlier_results() -> None:
    extra = sp.deliver_inputs(deliver(), results={}, earlier={"run_tests": {"failed": 24}}, user_message="")
    assert extra == {"upstream": {"run_tests": {"failed": 24}}}


def test_deliver_that_wrote_references_or_replies_directly_is_left_alone() -> None:
    assert sp.deliver_inputs(deliver(inputs={"s": "见 $run_tests.result"}), results={}, earlier={"run_tests": 1}, user_message="x") is None
    assert sp.deliver_inputs(deliver(inputs={"request": "你好"}), results={}, earlier={"run_tests": 1}, user_message="x") is None
    assert sp.deliver_inputs(atomic("p", role="deliver"), results={}, earlier={"run_tests": 1}, user_message="x") is None, "program 节点不改"


def test_the_patch_is_asked_at_start() -> None:
    # 之后的版本分支（q87、n04、n16）追加在它后面；它自己追加在它之前的最后一个后面，位置不能再动。
    assert AT_START.index(REFERENCES_PATCH) == AT_START.index(INTERRUPT_WITHDRAW_PATCH) + 1


# --- 工作流 ---


async def test_round_two_can_reference_round_one_inside_text(harness: StageHarness) -> None:
    s = harness.script
    s.behaviors["diagnose"] = returns({"root_cause": "缺少 psycopg"})
    s.plans["main"] = [
        stage(1, atomic("diagnose")),
        stage(2, deliver(inputs={"summary": "根因：$diagnose.result.root_cause"})),
    ]
    s.deliver["main"] = "answer"
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert s.call("answer").inp.node["inputs"] == {"summary": "根因：缺少 psycopg"}
    second = s.plan_inputs[1]
    assert second.earlier_results is not None and "diagnose" in second.earlier_results, "Planner 知道之前各轮有哪些结果可以引用"
    assert s.plan_inputs[0].earlier_results == {}


async def test_a_deliver_without_inputs_gets_the_earlier_results(harness: StageHarness) -> None:
    s = harness.script
    s.behaviors["run_tests"] = returns({"passed": 0, "errors": 24, "cause": "缺少 psycopg"})
    s.plans["main"] = [stage(1, atomic("run_tests")), stage(2, deliver())]
    s.deliver["main"] = "answer"
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    inputs = s.call("answer").inp.node["inputs"]
    assert inputs["upstream"] == {"run_tests": {"passed": 0, "errors": 24, "cause": "缺少 psycopg"}}
    assert inputs["user_message"] == inp.message


async def test_an_unresolvable_embedded_reference_fails_the_node(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, atomic("diagnose"), deliver(after=["diagnose"], inputs={"summary": "根因：$diagnose.result.root_cause，$apply_fix.result"}))]
    s.deliver["main"] = "answer"
    inp, _ = await harness.run()
    review = s.review(inp.task_id, 1)
    # 校验先拦下：本 Stage 没有 apply_fix，之前也没有，整个 Stage 不执行，Reviewer 看得到原因。
    assert all(r.status == "skipped" for r in review.stage_results.values())
    assert "apply_fix" in review.stage_results["answer"].error
