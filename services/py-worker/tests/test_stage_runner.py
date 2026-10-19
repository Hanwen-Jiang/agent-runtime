"""S-04 验收：Stage Runner 调度与展开（设计书 3.2.3；附录 B.2、B.3、D.5、E、F.1；计划 S-04）。

- 附录 B.3 的三个 Round 作为集成测试：DAG 的执行顺序与并行度符合预期。
- 每种 quality 与每种 mode 至少一个测试；revise 达到 max_rounds 后停止；search 的剪枝生效。
- 嵌套深度、扇出上限、只读约束的拒绝路径；单一写者；只传 result；Hook 挂载点；write_irreversible 前的人工审批。
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from agentruntime.common.v1 import enums_pb2
from agentruntime.workflow.v1 import task_pb2
from google.protobuf import json_format

from agentruntime_worker import names
from agentruntime_worker.activities.types import (
    StagePolicy,
    TemplateExpansion,
    TemplateInfo,
    ToolLoopStep,
)
from agentruntime_worker.workflows import stage_plan as sp

from .stage_harness import (  # noqa: F401 — harness 是 fixture
    StageHarness,
    barrier,
    blocks_forever,
    dumps,
    fails,
    harness,
    returns,
    sequence,
    slow,
)
from .support import SCHEMAS_DIR, assert_valid

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

QUARTER = {"constraints": {"quarter": "2026Q3"}}


def stage_example(name: str) -> dict[str, Any]:
    data = json.loads((SCHEMAS_DIR / "examples" / "stage.examples.json").read_text("utf-8"))
    return copy.deepcopy(next(e["instance"] for e in data["valid"] if e["name"] == name))


def atomic(node_id: str, *, after: list[str] | None = None, effect: str = "none", executor: str = "program", role: str | None = None, **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "kind": "atomic",
        "template": extra.pop("template", "step@v1"),
        "executor": executor,
        "effect": effect,
        "role": role or ("act" if effect in sp.WRITE_EFFECTS else "transform"),
        **extra,
    }
    if executor == "model":
        node.setdefault("output", "generate")
    if effect in sp.WRITE_EFFECTS:
        node.setdefault("idempotency_key", f"idk_{node_id}")
    if after is not None:
        node["after"] = after
    return node


def goal(node_id: str, mode: str | None, **extra: Any) -> dict[str, Any]:
    node = {"id": node_id, "kind": "goal", "goal": f"完成 {node_id}", "done_when": "给出结果", "output_schema": "result_v1", **extra}
    if mode is not None:
        node["mode"] = mode
    return node


def deliver(node_id: str = "answer", **extra: Any) -> dict[str, Any]:
    return atomic(node_id, executor="model", role="deliver", template="answer@v1", **extra)


def stage(seq: int, *nodes: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"seq": seq, "goal": f"第 {seq} 轮", "exit_when": "完成", "nodes": list(nodes), **extra}


def one_round(h: StageHarness, *nodes: dict[str, Any], deliver_id: str = "answer", scenario: str = "main", **extra: Any) -> None:
    h.script.plans[scenario] = [stage(1, *nodes, **extra)]
    h.script.deliver[scenario] = deliver_id


def results(h: StageHarness, task_id: str, round_seq: int = 1) -> dict[str, Any]:
    return h.script.review(task_id, round_seq).stage_results


def ids_of(calls: list[Any]) -> list[str]:
    return [c.node_id for c in calls]


# --- 附录 B.3：三个 Round ---


async def test_appendix_b3_three_rounds(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [
        stage_example("appendix_b3_round1_parallel_fetch"),
        stage_example("appendix_b2_stage"),
        stage_example("appendix_b3_round3_deliver"),
    ]
    s.deliver["main"] = "write_report"
    for t in ("sql_query@v3", "ticket_stats@v1"):
        s.behaviors[t] = barrier("round1", 3)
    reasons = {"reasons": [{"name": "物流延迟", "share": 0.6, "examples": ["T-1"]}]}
    s.tool_steps["read_tickets"] = lambda inp: ToolLoopStep(action="finish", result=reasons)
    s.behaviors["check_numbers_match@v1"] = sequence({"pass": False, "feedback": "数字对不上"}, {"pass": True})
    s.behaviors["report_rubric@v1"] = returns({"pass": True})
    s.behaviors["write_report"] = returns({"report": "华东退款率上升主要因为物流延迟"})

    inp, result = await harness.run(extra_inputs=QUARTER)
    task_id = inp.task_id

    assert result.status == enums_pb2.TASK_STATUS_DONE, result
    assert result.rounds == 3 and result.deliver_node_id == "write_report"
    assert json_format.MessageToDict(result.deliverable.value) == {"report": "华东退款率上升主要因为物流延迟"}

    # Round 1：三个查询互不依赖，并行执行（barrier 等齐三个才返回），$task 引用解析成 Task State 的约束。
    r1 = [s.call(n) for n in ("q_region", "q_sku", "q_reason")]
    assert all(c.outcome == "ok" for c in r1), "三个查询应当并行执行"
    assert all(c.task_queue == names.PROGRAM_QUEUE for c in r1)
    assert s.call("q_region").inp.node["inputs"] == {"params": {"quarter": "2026Q3"}}
    assert set(results(harness, task_id, 1)) == {"q_region", "q_sku", "q_reason"}

    # Round 2：read_tickets 是 sub_agent，在子工作流里跑完，analyze 才开始；analyze 的 revise 展开为 生成 → judge → 带反馈重来。
    child = sp.subworkflow_id(task_id, 2, "read_tickets")
    step = next(c for c in s.calls if c.activity == names.TOOL_LOOP_STEP and c.workflow_id == child)
    assert step.inp.isolated is True
    gens_and_judges = [s.call(f"analyze.{i}") for i in range(1, 5)]
    assert [c.start for c in gens_and_judges] == sorted(c.start for c in gens_and_judges)
    assert step.start < gens_and_judges[0].start, "analyze 依赖 read_tickets"
    assert s.call("analyze.2").inp.node["role"] == "judge" and s.call("analyze.2").inp.node["template"] == "check_numbers_match@v1"
    assert s.call("analyze.2").inp.node["inputs"]["candidate"] == {"node": "analyze.1", "inputs": {}}
    assert s.call("analyze.3").inp.node["inputs"]["feedback"] == {"pass": False, "feedback": "数字对不上"}
    r2 = results(harness, task_id, 2)
    assert set(r2) == {"read_tickets", "analyze"}, "Reviewer 只看顶层 Node，不看展开出的子 Node"
    assert r2["read_tickets"].result == reasons
    assert r2["analyze"].quality == {"type": "revise", "rounds": 2, "passed": True}
    assert r2["analyze"].result == {"node": "analyze.3", "inputs": {"feedback": {"pass": False, "feedback": "数字对不上"}}}

    # Round 3：deliver 节点的 revise，judge 一次通过。
    assert ids_of(s.executed(task_id)[-2:]) == ["write_report.1", "write_report.2"]
    assert "RATIONALE" not in dumps([r.stage_results for r in s.reviews if r.task_id == task_id]), "rationale 只写入 trace"


# --- 调度 ---


async def test_ready_nodes_start_in_id_order_and_respect_the_fan_out_limit(harness: StageHarness) -> None:
    s = harness.script
    s.policy = StagePolicy(max_parallel=2)
    nodes = [atomic(f"n{i}") for i in (4, 1, 6, 3, 2, 5)]
    for n in nodes:
        s.behaviors[n["id"]] = slow(0.3)
    one_round(harness, *nodes, deliver("answer", after=[n["id"] for n in nodes]))
    inp, handle = await harness.start()
    result = await asyncio.wait_for(handle.result(), timeout=60)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    # 调度顺序看事件历史里 activity 的排程顺序（这是重放要对上的东西）；Worker 取任务的先后不保证。
    assert (await scheduled_nodes(handle))[:6] == [f"n{i}" for i in range(1, 7)], "ready 集合按 Node id 排序后启动"
    assert s.max_running == 2, f"同时执行的 Node 不超过上限 2，实际最多 {s.max_running}"
    assert list(results(harness, inp.task_id)) == sorted(results(harness, inp.task_id)), "结果按 Node id 排序后合并"


async def scheduled_nodes(handle: Any) -> list[str]:
    """事件历史里执行器 activity 的排程顺序（Node id）。"""
    history = await handle.fetch_history()
    out = []
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            attrs = event.activity_task_scheduled_event_attributes
            if attrs.activity_type.name in (names.EXECUTE_MODEL_NODE, names.EXECUTE_PROGRAM_NODE):
                out.append(json.loads(attrs.input.payloads[0].data)["node"]["id"])
    return out


async def test_downstream_nodes_receive_only_upstream_results(harness: StageHarness) -> None:
    s = harness.script
    s.behaviors["a"] = returns({"value": 7, "secret_free": True})
    one_round(harness, atomic("a"), atomic("b", after=["a"], inputs={"v": "$a.result.value", "all": "$a.result"}), deliver(after=["b"], inputs={"b": "$b.result"}))
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert s.call("b").inp.node["inputs"] == {"v": 7, "all": {"value": 7, "secret_free": True}}
    assert "RATIONALE" not in dumps([c.inp for c in s.calls]), "下游只拿 result，rationale 不往下传"


async def test_upstream_failure_skips_dependents(harness: StageHarness) -> None:
    s = harness.script
    s.behaviors["a"] = fails("查询失败")
    one_round(harness, atomic("a"), atomic("b", after=["a"]), atomic("c"), deliver(after=["c"]))
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE, "失败交给 Reviewer 判定，不让 Task 直接失败"
    r = results(harness, inp.task_id)
    assert r["a"].status == "failed" and "查询失败" in r["a"].error
    assert r["b"].status == "skipped" and r["b"].failure_class == "upstream_failed"
    assert r["c"].status == "succeeded"
    assert "b" not in ids_of(s.executed(inp.task_id))


async def test_failed_deliver_node_goes_to_the_reviewer(harness: StageHarness) -> None:
    harness.script.behaviors["answer"] = fails("模型报错")
    one_round(harness, deliver())
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_ABORTED and result.final_verdict == enums_pb2.VERDICT_ABORT
    assert results(harness, inp.task_id)["answer"].status == "failed"
    assert len([f for f in harness.script.finalized if f.task_id == inp.task_id]) == 1


# --- 单一写者与 effect ---


async def test_write_nodes_never_run_at_the_same_time(harness: StageHarness) -> None:
    s = harness.script
    s.policy = StagePolicy(max_parallel=4)
    for n in ("w1", "w2", "r1", "r2"):
        s.behaviors[n] = slow(0.3)
    one_round(
        harness,
        atomic("w1", effect="write_reversible"),
        atomic("w2", effect="write_reversible"),
        atomic("r1", effect="read", role="fetch"),
        atomic("r2", effect="read", role="fetch"),
        deliver(after=["w1", "w2", "r1", "r2"]),
    )
    inp, result = await harness.run(effect_ceiling=enums_pb2.EFFECT_WRITE_REVERSIBLE)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    w1, w2, r1, r2 = (s.call(n) for n in ("w1", "w2", "r1", "r2"))
    assert w1.end < w2.start or w2.end < w1.start, "同一时刻只允许一个写者"
    assert r1.start < r2.end and r2.start < r1.end, "读节点照常并行"


async def test_effect_above_the_task_ceiling_is_denied(harness: StageHarness) -> None:
    one_round(harness, atomic("w", effect="write_reversible"), deliver())
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_READ)
    r = results(harness, inp.task_id)
    assert r["w"].status == "failed" and r["w"].failure_class == "denied"
    assert "w" not in ids_of(harness.script.executed(inp.task_id))


async def wait_at_gate(h: StageHarness, task_id: str, node_id: str) -> None:
    """审批 gate 在 before_node Hook 之后；看到 Hook 调用后再稍等，确保 gate 已经在等回复。"""
    await h.wait_for(
        lambda: any(c.task_id == task_id and c.mount == "before_node" and c.subject.get("node", {}).get("id") == node_id for c in h.script.hook_calls),
        f"{node_id} 的 before_node Hook",
    )
    await asyncio.sleep(1)


async def test_write_irreversible_waits_for_human_approval(harness: StageHarness) -> None:
    s = harness.script
    one_round(harness, atomic("pay_ticket", effect="write_irreversible"), deliver(after=["pay_ticket"]))
    inp, handle = await harness.start(effect_ceiling=enums_pb2.EFFECT_WRITE_IRREVERSIBLE)
    await wait_at_gate(harness, inp.task_id, "pay_ticket")
    assert "pay_ticket" not in ids_of(s.executed(inp.task_id)), "审批通过之前不执行"
    await handle.signal(names.HUMAN_REPLY_SIGNAL, task_pb2.HumanReplySignal(question_id="pay_ticket.1", option="approve", replied_by_user_id="u7"))
    result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    gate = s.call("pay_ticket").inp.gate
    assert gate is not None and (gate.gate_node, gate.option, gate.approved_by) == ("pay_ticket.1", "approve", "u7")


async def test_rejected_approval_denies_the_write(harness: StageHarness) -> None:
    s = harness.script
    one_round(harness, atomic("pay_ticket", effect="write_irreversible"), deliver(after=["pay_ticket"]))
    inp, handle = await harness.start(effect_ceiling=enums_pb2.EFFECT_WRITE_IRREVERSIBLE)
    await wait_at_gate(harness, inp.task_id, "pay_ticket")
    await handle.signal(names.HUMAN_REPLY_SIGNAL, task_pb2.HumanReplySignal(question_id="pay_ticket.1", option="reject"))
    await asyncio.wait_for(handle.result(), timeout=30)
    r = results(harness, inp.task_id)
    assert r["pay_ticket"].status == "failed" and r["pay_ticket"].failure_class == "denied"
    assert r["answer"].status == "skipped"
    assert "pay_ticket" not in ids_of(s.executed(inp.task_id))


# --- Hook ---


def hook_trace(h: StageHarness, task_id: str) -> list[tuple[str, str, list[str]]]:
    return [(c.mount, c.subject.get("node", {}).get("id", "stage"), c.hooks) for c in h.script.hook_calls if c.task_id == task_id]


async def test_hooks_run_at_stage_and_node_mount_points(harness: StageHarness) -> None:
    one_round(harness, atomic("a", effect="read", role="fetch"), deliver(after=["a"]), hooks={"before": ["budget_check"], "after": ["checkpoint"]})
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert hook_trace(harness, inp.task_id) == [
        ("before_stage", "stage", ["budget_check"]),
        ("before_node", "a", ["budget_check"]),
        ("after_node", "a", ["trace_result", "audit_log"]),
        ("before_node", "answer", ["budget_check"]),
        ("after_node", "answer", ["audit_log"]),
        ("after_stage", "stage", ["checkpoint"]),
    ]
    before_a = next(c for c in harness.script.hook_calls if c.task_id == inp.task_id and c.mount == "before_node")
    assert before_a.tenant.tenant_id == "t1"
    after_a = next(c for c in harness.script.hook_calls if c.task_id == inp.task_id and c.mount == "after_node")
    assert after_a.subject["result"] == {"node": "a", "inputs": {}}
    assert "RATIONALE" not in dumps(after_a.subject)


async def test_before_node_deny_marks_the_node_denied(harness: StageHarness) -> None:
    harness.script.hook_overrides[("before_node", "a")] = {"decision": "deny", "reason": "超出预算"}
    one_round(harness, atomic("a"), atomic("b", after=["a"]), deliver())
    inp, _ = await harness.run()
    r = results(harness, inp.task_id)
    assert r["a"].status == "failed" and r["a"].failure_class == "denied" and "超出预算" in r["a"].error
    assert r["b"].status == "skipped" and r["b"].failure_class == "upstream_failed"
    assert "a" not in ids_of(harness.script.executed(inp.task_id)), "被拒绝的 Node 不执行，也不重试"


async def test_modify_hooks_rewrite_inputs_and_outputs(harness: StageHarness) -> None:
    s = harness.script
    s.hook_overrides[("before_node", "a")] = {"decision": "modify", "modification": {"target": "input", "patch": [{"op": "replace", "path": "/email", "value": "***"}]}}
    s.hook_overrides[("after_node", "a")] = {"decision": "modify", "modification": {"target": "output", "patch": [{"op": "remove", "path": "/inputs"}]}}
    one_round(harness, atomic("a", inputs={"email": "a@example.com", "q": 1}), atomic("b", after=["a"], inputs={"from_a": "$a.result"}), deliver())
    inp, _ = await harness.run()
    assert s.call("a").inp.node["inputs"] == {"email": "***", "q": 1}, "before_node 的 modify 改写输入（脱敏）"
    assert s.call("b").inp.node["inputs"] == {"from_a": {"node": "a"}}, "after_node 的 modify 改写输出，下游拿到改写后的结果"
    assert results(harness, inp.task_id)["a"].result == {"node": "a"}


async def test_inject_hook_reaches_the_executor(harness: StageHarness) -> None:
    harness.script.hook_overrides[("before_node", "answer")] = {"decision": "inject", "injection": {"zone": "brief", "content": "只用公开数据"}}
    one_round(harness, deliver())
    await harness.run()
    assert harness.script.call("answer").inp.injections == [{"zone": "brief", "content": "只用公开数据"}]


async def test_before_stage_deny_skips_every_node(harness: StageHarness) -> None:
    harness.script.hook_overrides[("before_stage", "stage")] = {"decision": "deny", "reason": "租户预算用尽"}
    one_round(harness, atomic("a"), deliver(), hooks={"before": ["budget_check"]})
    inp, _ = await harness.run()
    r = results(harness, inp.task_id)
    assert all(v.status == "skipped" and v.failure_class == "denied" and "租户预算用尽" in v.error for v in r.values())
    assert harness.script.executed(inp.task_id) == []


async def test_after_stage_deny_withholds_the_results(harness: StageHarness) -> None:
    harness.script.hook_overrides[("after_stage", "stage")] = {"decision": "deny", "reason": "检查点写入失败"}
    one_round(harness, deliver(), hooks={"after": ["checkpoint"]})
    inp, result = await harness.run()
    r = results(harness, inp.task_id)
    assert r["answer"].status == "failed" and r["answer"].failure_class == "denied" and r["answer"].result is None
    assert result.status == enums_pb2.TASK_STATUS_ABORTED


# --- quality ---


async def test_revise_stops_at_max_rounds(harness: StageHarness) -> None:
    s = harness.script
    s.behaviors["strict_judge@v1"] = returns({"pass": False, "feedback": "还不够好"})
    one_round(harness, deliver(quality={"type": "revise", "judge": "strict_judge@v1", "max_rounds": 3}))
    inp, result = await harness.run()
    assert ids_of(s.executed(inp.task_id)) == [f"answer.{i}" for i in range(1, 7)], "3 轮：生成与 judge 交替，达到 max_rounds 后停止"
    r = results(harness, inp.task_id)["answer"]
    assert r.status == "succeeded" and r.quality == {"type": "revise", "rounds": 3, "passed": False}
    assert r.result["node"] == "answer.5", "达到上限时交出最后一次生成的结果，由 Reviewer 判定"
    judge_inputs = s.call("answer.2").inp.node["inputs"]
    assert judge_inputs["candidate"] == {"node": "answer.1", "inputs": {}} and "RATIONALE" not in dumps(judge_inputs), "judge 不看生成者的 rationale"


async def test_ensemble_vote(harness: StageHarness) -> None:
    s = harness.script
    s.behaviors["answer.1"] = returns({"answer": "A"})
    s.behaviors["answer.2"] = returns({"answer": "B"})
    s.behaviors["answer.3"] = returns({"answer": "A"})
    one_round(harness, deliver(quality={"type": "ensemble", "n": 3, "aggregate": "vote"}))
    inp, result = await harness.run()
    assert sorted(ids_of(s.executed(inp.task_id))) == ["answer.1", "answer.2", "answer.3"]
    assert [s.call(f"answer.{i}").inp.node["inputs"]["sample_index"] for i in (1, 2, 3)] == [1, 2, 3]
    r = results(harness, inp.task_id)["answer"]
    assert r.result == {"answer": "A"}
    assert r.quality == {"type": "ensemble", "n": 3, "aggregate": "vote", "chosen": "answer.1"}
    assert json_format.MessageToDict(result.deliverable.value) == {"answer": "A"}


async def test_ensemble_judge_picks_the_best(harness: StageHarness) -> None:
    s = harness.script
    for i, text in enumerate(("短", "中等", "最好"), start=1):
        s.behaviors[f"answer.{i}"] = returns({"answer": text})
    s.behaviors["pick_best@v1"] = returns({"best": 3})
    one_round(harness, deliver(quality={"type": "ensemble", "n": 3, "aggregate": "judge", "judge": "pick_best@v1"}))
    inp, _ = await harness.run()
    judge = s.call("answer.4")
    assert judge.inp.node["role"] == "judge"
    assert judge.inp.node["inputs"]["candidates"] == [{"answer": "短"}, {"answer": "中等"}, {"answer": "最好"}]
    r = results(harness, inp.task_id)["answer"]
    assert r.result == {"answer": "最好"} and r.quality["chosen"] == "answer.3"


async def test_search_prunes_to_the_beam(harness: StageHarness) -> None:
    s = harness.script
    s.templates["score_plan@v1"] = TemplateInfo(executor="model", effect="none", output="score")
    s.behaviors["score_plan@v1"] = sequence({"scores": [0.1, 0.9, 0.5]}, {"scores": [0.3, 0.95, 0.2]})
    one_round(harness, deliver(quality={"type": "search", "expand_k": 3, "max_depth": 2, "beam_width": 1, "scorer": "score_plan@v1"}))
    inp, result = await harness.run()
    executed = ids_of(s.executed(inp.task_id))
    assert executed.count("answer.4") == 1 and executed.count("answer.8") == 1, "每层一个 scorer"
    assert sorted(executed) == sorted(f"answer.{i}" for i in range(1, 9))
    states = [s.call(f"answer.{i}").inp.node["inputs"]["state"] for i in (5, 6, 7)]
    assert all(state == {"node": "answer.2", "inputs": {"state": None, "depth": 1, "branch": 2}} for state in states), "剪掉的候选不再扩展"
    assert s.call("answer.8").inp.node["inputs"]["candidates"][1]["node"] == "answer.6"
    r = results(harness, inp.task_id)["answer"]
    assert r.result["node"] == "answer.6" and r.quality == {"type": "search", "depth": 2, "chosen": "answer.6"}
    assert result.status == enums_pb2.TASK_STATUS_DONE


# --- Goal Node ---


REPORT_RECIPE = TemplateExpansion(
    nodes=[
        atomic("fetch", inputs={"quarter": "$goal.quarter"}, effect="read", role="fetch"),
        atomic("summarize", after=["fetch"], executor="model", inputs={"rows": "$fetch.result"}),
    ],
    output="summarize",
)


async def test_template_goal_expands_inline(harness: StageHarness) -> None:
    s = harness.script
    s.recipes["report@v1"] = REPORT_RECIPE
    one_round(harness, goal("report", "template", recipe="report@v1", inputs={"quarter": "$task.constraints.quarter"}), deliver(after=["report"], inputs={"r": "$report.result"}))
    inp, result = await harness.run(extra_inputs=QUARTER)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert ids_of(s.executed(inp.task_id)) == ["report.1", "report.2", "answer"]
    assert s.call("report.1").inp.node["inputs"] == {"quarter": "2026Q3"}
    assert s.call("report.2").inp.node["inputs"] == {"rows": {"node": "report.1", "inputs": {"quarter": "2026Q3"}}}
    assert results(harness, inp.task_id)["report"].result == {"node": "report.2", "inputs": {"rows": {"node": "report.1", "inputs": {"quarter": "2026Q3"}}}}
    assert all(c.workflow_id == inp.task_id for c in s.executed(inp.task_id)), "小子图内联执行，不开子工作流"


async def test_large_template_is_split_into_child_workflows(harness: StageHarness) -> None:
    s = harness.script
    s.policy = StagePolicy(max_parallel=4, max_inline_nodes=6, batch_size=3, batch_parallel=2)
    items = [{"id": f"T-{i}"} for i in range(1, 11)]
    s.recipes["ticket_triage@v2"] = TemplateExpansion(
        per_item=[atomic("classify", inputs={"ticket": "$item.id"}), atomic("extract", after=["classify"], inputs={"label": "$classify.result.node"})],
        items=items,
        collect="extract",
    )
    one_round(harness, goal("triage", "template", recipe="ticket_triage@v2"), deliver(after=["triage"]))
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    batches = sorted({c.workflow_id for c in s.executed(inp.task_id) if c.node_id.startswith("triage.")})
    assert batches == [sp.subworkflow_id(inp.task_id, 1, "triage", f"batch-{b}") for b in range(1, 5)], "10 个条目、每批 3 个，拆成 4 个子工作流"
    assert sorted(c.node_id for c in s.executed(inp.task_id) if c.node_id.startswith("triage.")) == sorted(
        f"triage.{i}.{j}" for i in range(1, 11) for j in (1, 2)
    )
    assert s.call("triage.7.1").inp.node["inputs"] == {"ticket": "T-7"}
    r = results(harness, inp.task_id)["triage"]
    assert [x["node"] for x in r.result["items"]] == [f"triage.{i}.2" for i in range(1, 11)], "按条目顺序汇总"


async def test_fixed_template_larger_than_the_inline_limit_is_rejected(harness: StageHarness) -> None:
    s = harness.script
    s.policy = StagePolicy(max_inline_nodes=2)
    s.recipes["big@v1"] = TemplateExpansion(nodes=[atomic(f"s{i}") for i in range(3)], output="s2")
    one_round(harness, goal("big", "template", recipe="big@v1"), deliver())
    inp, _ = await harness.run()
    r = results(harness, inp.task_id)["big"]
    assert r.status == "failed" and r.failure_class == "denied" and "上限" in r.error
    assert not [c for c in s.executed(inp.task_id) if c.node_id.startswith("big.")]


def scripted_steps(*steps: ToolLoopStep):
    def step(inp: Any) -> ToolLoopStep:
        return steps[min(inp.iteration, len(steps)) - 1]

    return step


async def test_tool_loop_runs_each_step_as_an_activity(harness: StageHarness) -> None:
    s = harness.script
    s.tool_steps["fix"] = scripted_steps(
        ToolLoopStep(action="tool", tool="repo_read", args={"path": "a.py"}),
        ToolLoopStep(action="finish", result={"patch": "diff"}),
    )
    s.tool_results["repo_read"] = lambda i: {"content": "print(1)"}
    one_round(harness, goal("fix", "tool_loop", tools=["repo_read"], max_iterations=5), deliver(after=["fix"]))
    inp, _ = await harness.run()
    loop = [(c.activity, c.node_id) for c in s.calls if c.workflow_id == inp.task_id and c.node_id.startswith("fix.")]
    assert loop == [(names.TOOL_LOOP_STEP, "fix.1"), (names.CALL_TOOL, "fix.2"), (names.TOOL_LOOP_STEP, "fix.3")]
    second = s.call("fix.3").inp
    assert second.iteration == 2 and second.history == [{"tool": "repo_read", "args": {"path": "a.py"}, "observation": {"content": "print(1)"}}]
    assert results(harness, inp.task_id)["fix"].result == {"patch": "diff"}


async def test_tool_loop_stops_at_max_iterations(harness: StageHarness) -> None:
    s = harness.script
    s.tool_steps["fix"] = scripted_steps(ToolLoopStep(action="tool", tool="repo_read", args={}))
    one_round(harness, goal("fix", "tool_loop", tools=["repo_read"], max_iterations=3), deliver())
    inp, _ = await harness.run()
    steps = [c for c in s.calls if c.activity == names.TOOL_LOOP_STEP and c.workflow_id == inp.task_id]
    assert len(steps) == 3
    r = results(harness, inp.task_id)["fix"]
    assert r.status == "failed" and "max_iterations" in r.error


async def test_tool_loop_breaks_on_the_same_error_repeating(harness: StageHarness) -> None:
    s = harness.script
    s.tool_steps["fix"] = scripted_steps(ToolLoopStep(action="tool", tool="run_tests", args={}))
    s.tool_results["run_tests"] = lambda i: RuntimeError("AssertionError: 1 != 2")
    one_round(harness, goal("fix", "tool_loop", tools=["run_tests"], max_iterations=20), deliver())
    inp, _ = await harness.run()
    tool_calls = [c for c in s.calls if c.activity == names.CALL_TOOL and c.workflow_id == inp.task_id]
    assert len(tool_calls) == sp.REPEATED_ERROR_LIMIT, "同一错误连续出现 3 次就打断，不等到 max_iterations"
    r = results(harness, inp.task_id)["fix"]
    assert r.status == "failed" and "repeated_error" in r.error


async def test_tool_loop_tool_above_the_ceiling_is_denied(harness: StageHarness) -> None:
    s = harness.script
    s.tools["repo_write"] = "write_reversible"
    one_round(harness, goal("fix", "tool_loop", tools=["repo_write"], effect_ceiling="read"), deliver())
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_WRITE_REVERSIBLE)
    r = results(harness, inp.task_id)["fix"]
    assert r.status == "failed" and r.failure_class == "denied"
    assert not [c for c in s.calls if c.activity == names.TOOL_LOOP_STEP and c.workflow_id == inp.task_id]


async def test_sub_agent_runs_in_a_read_only_child_workflow(harness: StageHarness) -> None:
    s = harness.script
    s.tools["web_search"] = "read"
    s.tool_steps["research"] = scripted_steps(
        ToolLoopStep(action="tool", tool="web_search", args={"q": "市场规模"}),
        ToolLoopStep(action="finish", result={"size": "120 亿"}),
    )
    one_round(harness, goal("research", "sub_agent", tools=["web_search"]), deliver(after=["research"]))
    inp, _ = await harness.run()
    child_id = sp.subworkflow_id(inp.task_id, 1, "research")
    child = harness.client.get_workflow_handle(child_id)
    desc = await child.describe()
    assert desc.workflow_type == names.SUB_AGENT_WORKFLOW
    assert (await desc.memo()).get("agentruntime_scope") == "t1", "子工作流也记下所属租户"
    in_child = [c for c in s.calls if c.workflow_id == child_id]
    assert [c.activity for c in in_child] == [names.TOOL_LOOP_STEP, names.CALL_TOOL, names.TOOL_LOOP_STEP]
    assert all(c.inp.isolated for c in in_child if c.activity == names.TOOL_LOOP_STEP)
    assert results(harness, inp.task_id)["research"].result == {"size": "120 亿"}, "只交回结果，不交回中间步骤"


async def test_sub_agent_with_a_write_tool_is_rejected(harness: StageHarness) -> None:
    s = harness.script
    s.tools["db_write"] = "write_reversible"
    one_round(harness, goal("research", "sub_agent", tools=["db_write"]), deliver())
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_WRITE_IRREVERSIBLE)
    r = results(harness, inp.task_id)["research"]
    assert r.status == "failed" and r.failure_class == "denied" and "只读" in r.error
    with pytest.raises(Exception):
        await harness.client.get_workflow_handle(sp.subworkflow_id(inp.task_id, 1, "research")).describe()


async def test_sub_task_runs_a_nested_task_loop(harness: StageHarness) -> None:
    s = harness.script
    s.plans["child"] = [stage(1, deliver("child_answer"))]
    s.deliver["child"] = "child_answer"
    s.behaviors["child_answer"] = returns({"summary": "子任务完成"})
    one_round(harness, goal("investigate", "sub_task", inputs={"scenario": "child", "topic": "$task.constraints.quarter"}), deliver(after=["investigate"]))
    inp, result = await harness.run(extra_inputs=QUARTER)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    child_plan = next(p for p in s.plan_inputs if p.task_id != inp.task_id and s.scenario_of[p.task_id] == "child")
    child = await harness.client.get_workflow_handle(child_plan.task_id).describe()
    assert child.workflow_type == names.TASK_WORKFLOW
    child_input = next(iter([c for c in s.calls if c.workflow_id == child_plan.task_id]))
    assert child_input.inp.task_id == child_plan.task_id
    assert child_plan.message == "完成 investigate"
    assert child_plan.inputs["topic"] == "2026Q3"
    assert child_plan.pinned == next(p for p in s.plan_inputs if p.task_id == inp.task_id).pinned, "子 Task 沿用父 Task 固定的版本"
    assert results(harness, inp.task_id)["investigate"].result == {"summary": "子任务完成"}


async def test_sub_task_beyond_the_depth_limit_is_rejected(harness: StageHarness) -> None:
    one_round(harness, goal("investigate", "sub_task"), deliver())
    inp, _ = await harness.run(depth=sp.MAX_SUB_TASK_DEPTH)
    r = results(harness, inp.task_id)["investigate"]
    assert r.status == "failed" and r.failure_class == "denied" and "深度" in r.error
    assert [p for p in harness.script.plan_inputs if p.task_id != inp.task_id] == []


async def test_mode_defaults_to_template_with_a_recipe_else_tool_loop(harness: StageHarness) -> None:
    s = harness.script
    s.recipes["report@v1"] = REPORT_RECIPE
    s.tool_steps["explore"] = scripted_steps(ToolLoopStep(action="finish", result={"found": True}))
    one_round(harness, goal("report", None, recipe="report@v1", inputs={"quarter": "Q"}), goal("explore", None), deliver())
    inp, _ = await harness.run()
    assert [c.node_id for c in s.calls if c.activity == names.EXPAND_TEMPLATE and c.workflow_id == inp.task_id] == ["report"]
    assert [c.node_id for c in s.calls if c.activity == names.TOOL_LOOP_STEP and c.workflow_id == inp.task_id] == ["explore.1"]


async def test_quality_wraps_a_goal_node(harness: StageHarness) -> None:
    s = harness.script
    attempts = iter([ToolLoopStep(action="finish", result={"patch": "v1"}), ToolLoopStep(action="finish", result={"patch": "v2"})])
    s.tool_steps["fix"] = lambda inp: next(attempts)
    s.behaviors["run_tests@v1"] = sequence({"pass": False, "feedback": "测试失败"}, {"pass": True})
    one_round(harness, goal("fix", "tool_loop", quality={"type": "revise", "judge": "run_tests@v1", "max_rounds": 3}), deliver())
    inp, _ = await harness.run()
    steps = [c.node_id for c in s.calls if c.activity == names.TOOL_LOOP_STEP and c.workflow_id == inp.task_id]
    assert steps == ["fix.1.1", "fix.3.1"], "每一轮生成是 Goal 的一个副本，子 Node 再往下编号"
    assert [c.node_id for c in s.executed(inp.task_id) if c.node_id.startswith("fix")] == ["fix.2", "fix.4"]
    r = results(harness, inp.task_id)["fix"]
    assert r.result == {"patch": "v2"} and r.quality == {"type": "revise", "rounds": 2, "passed": True}


# --- 取消与确定性 ---


async def test_cancel_stops_every_running_node(harness: StageHarness) -> None:
    s = harness.script
    for n in ("a", "b"):
        s.behaviors[n] = blocks_forever()
    # timeout_s 20 → heartbeat 1 秒，取消很快送到 activity。
    one_round(harness, atomic("a", budget={"timeout_s": 20}), atomic("b", budget={"timeout_s": 20}), deliver(after=["a", "b"]))
    inp, handle = await harness.start()
    await harness.wait_for(lambda: len(s.executed(inp.task_id)) == 2, "两个 Node 都在运行")
    await handle.cancel()
    with pytest.raises(Exception):
        await asyncio.wait_for(handle.result(), timeout=30)
    await harness.wait_for(lambda: all(c.outcome == "cancelled" for c in s.executed(inp.task_id)), "运行中的 Node 都被取消")
    assert len([f for f in s.finalized if f.task_id == inp.task_id]) == 1


async def test_expanded_ids_are_the_same_on_every_run(harness: StageHarness) -> None:
    s = harness.script
    s.recipes["report@v1"] = REPORT_RECIPE
    s.behaviors["strict_judge@v1"] = sequence({"pass": False}, {"pass": True}, {"pass": False}, {"pass": True})
    one_round(
        harness,
        goal("report", "template", recipe="report@v1", inputs={"quarter": "Q"}),
        deliver(after=["report"], quality={"type": "revise", "judge": "strict_judge@v1", "max_rounds": 2}),
    )
    first, _ = await harness.run()
    second, _ = await harness.run()
    assert sorted(ids_of(s.executed(first.task_id))) == sorted(ids_of(s.executed(second.task_id))) == [
        "answer.1", "answer.2", "answer.3", "answer.4", "report.1", "report.2",
    ]
