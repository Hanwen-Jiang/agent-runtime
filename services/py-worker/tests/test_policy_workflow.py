"""T-09 验收：Policy Engine 经真实的 Task 工作流与 Stage Runner 执行（细化见 docs/plan/policy-engine.md 第 2、3 节）。

Temporal 本地开发服务上跑真实的 Task 工作流；Planner、Reviewer、执行器用 stage_harness 的脚本，Hook 由真的 PolicyEngine 执行
（script.engine），或者用 hook_overrides 给出指定的结果。覆盖：
- 默认 Hook 与策略 Hook 的顺序与合并；Hook 的 subject（预算、幂等键、写者已串行化）。
- deny 不重试：被拒的 Node 不执行、Hook 只调一次。
- 按 Registry 核对 effect：写低、executor 不符、模板没登记、Catalog 取不到，都在审批与执行之前拒绝；写高照常；桩 Catalog 不核对。
- write_irreversible 的审批不受 Hook 影响。
- before_finish：驳回后回到 Planner；驳回满 2 次后问人。
- Task 级挂载点：on_task_received、on_task_start 的 deny；modify、inject 按 deny；on_task_end 只记录；before_compact 声明了就拒绝；
  租户策略取不到就拒绝。
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

from agentruntime.common.v1 import enums_pb2

from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider

from agentruntime_worker.activities.types import HookCall, NodeInput, StagePolicy, TemplateInfo
from agentruntime_worker.executor import keys
from agentruntime_worker.policy import PolicyEngine
from agentruntime_worker.reviewer.rules import HUMAN_REPLIES

from .stage_harness import Script, StageHarness, harness, returns, sequence  # noqa: F401 — harness 是 fixture
from .test_stage_runner import atomic, deliver, one_round, stage
from .test_task_verdicts import deliverable, executed_ids, out, reply_with, scripted

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

WRITE_CEILING = enums_pb2.EFFECT_WRITE_IRREVERSIBLE
ANSWER_SCHEMA = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string", "minLength": 1}}}


def use_engine(s: Script) -> ConsoleTelemetryProvider:
    telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
    s.engine = PolicyEngine(telemetry=telemetry)
    return telemetry


def calls_at(s: Script, mount: str, node_id: str | None = None) -> list[HookCall]:
    return [c for c in s.hook_calls if c.mount == mount and (node_id is None or c.subject.get("node", {}).get("id") == node_id)]


def hooks_at(s: Script, mount: str, node_id: str | None = None) -> list[str]:
    (only,) = calls_at(s, mount, node_id)
    return list(only.hooks)


def review_result(s: Script, task_id: str, node_id: str, round_seq: int = 1) -> Any:
    return s.review(task_id, round_seq).stage_results[node_id]


# --- 默认 Hook 与策略 Hook ---


async def test_default_hooks_run_through_the_real_engine(harness: StageHarness) -> None:
    s = harness.script
    use_engine(s)
    one_round(
        harness,
        atomic("q", role="fetch", effect="read"),
        atomic("w", effect="write_reversible", after=["q"]),
        deliver(after=["w"]),
    )
    inp, result = await harness.run(effect_ceiling=WRITE_CEILING)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert hooks_at(s, "before_node", "q") == ["budget_check"]
    assert hooks_at(s, "before_node", "w") == ["budget_check", "idempotency_check", "single_writer_lock"]
    assert hooks_at(s, "after_node", "q") == ["trace_result", "audit_log"]
    assert hooks_at(s, "after_node", "w") == ["checkpoint", "audit_log"]
    (before_w,) = calls_at(s, "before_node", "w")
    assert before_w.subject["budget"] == {"task_usd": 2.0, "spent_usd": 0.0}
    assert before_w.subject["write_serialized"] is True
    assert before_w.subject["idempotency_key"] == keys.idempotency_key("t1", inp.task_id, "w", before_w.subject["node"].get("inputs", {}))


async def test_policy_hooks_join_the_defaults_and_audit_stays_last(harness: StageHarness) -> None:
    s = harness.script
    use_engine(s)
    s.policy = StagePolicy(hooks={"after_node": ["pii_redaction"], "before_node": ["budget_check"]})
    s.behaviors["answer"] = returns({"text": "请联系 zhang.san@example.com"})
    one_round(harness, deliver())
    _, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert hooks_at(s, "before_node", "answer") == ["budget_check"], "同名的 Hook 只执行一次"
    assert hooks_at(s, "after_node", "answer") == ["pii_redaction", "audit_log"], "策略的 Hook 在默认 Hook 之后，audit_log 永远最后"
    text = deliverable(result)["text"]
    assert "zhang.san@example.com" not in text and "[REDACTED]" in text


async def test_policy_cannot_remove_default_hooks(harness: StageHarness) -> None:
    s = harness.script
    use_engine(s)
    s.policy = StagePolicy(hooks={"before_node": [], "after_node": []})
    one_round(harness, atomic("w", effect="write_reversible"), deliver(after=["w"]))
    await harness.run(effect_ceiling=WRITE_CEILING)
    assert hooks_at(s, "before_node", "w") == ["budget_check", "idempotency_check", "single_writer_lock"]


# --- deny 不重试 ---


async def test_a_node_denied_by_a_hook_is_neither_executed_nor_retried(harness: StageHarness) -> None:
    s = harness.script
    use_engine(s)
    one_round(harness, atomic("q", role="fetch", effect="read", budget={"max_cost_usd": 5.0}), deliver(after=["q"]))
    inp, result = await harness.run()
    assert "q" not in executed_ids(s, inp.task_id), "被拒的 Node 不调用执行器"
    assert len(calls_at(s, "before_node", "q")) == 1, "deny 不重试"
    q = review_result(s, inp.task_id, "q")
    assert (q.status, q.failure_class) == ("failed", "denied") and "budget_check" in q.error
    assert result.status == enums_pb2.TASK_STATUS_ABORTED


# --- 按 Registry 核对 effect ---


def registered(s: Script, **templates: TemplateInfo) -> None:
    s.catalog_authoritative = True
    s.templates = {"answer@v1": TemplateInfo(executor="model", effect="none", output="generate"), **templates}


async def test_a_node_declared_below_its_registered_effect_is_denied_before_approval(harness: StageHarness) -> None:
    s = harness.script
    registered(s, **{"send@v1": TemplateInfo(executor="program", effect="write_irreversible")})
    one_round(harness, atomic("send", template="send@v1", effect="none", role="transform"), deliver())
    inp, _ = await harness.run(effect_ceiling=WRITE_CEILING)
    assert "send@v1" in {t for r in s.catalog_requests for t in r.templates}, "Stage 开始时把 Atomic Node 的模板一并交给 Catalog"
    send = review_result(s, inp.task_id, "send")
    assert (send.status, send.failure_class) == ("failed", "denied") and "effect_mismatch" in send.error
    assert "send" not in executed_ids(s, inp.task_id)
    assert not s.questions, "被拒的 Node 不问人"
    assert not calls_at(s, "before_node", "send"), "核对在 before_node Hook 之前"


async def test_a_node_with_the_wrong_executor_is_denied(harness: StageHarness) -> None:
    s = harness.script
    registered(s, **{"step@v1": TemplateInfo(executor="model", effect="none", output="generate")})
    one_round(harness, atomic("s1"), deliver())
    inp, _ = await harness.run()
    s1 = review_result(s, inp.task_id, "s1")
    assert s1.failure_class == "denied" and "executor_mismatch" in s1.error
    assert "s1" not in executed_ids(s, inp.task_id)


async def test_a_node_declared_above_its_registered_effect_runs(harness: StageHarness) -> None:
    s = harness.script
    registered(s, **{"step@v1": TemplateInfo(executor="program", effect="none")})
    one_round(harness, atomic("s1", effect="read", role="fetch"), deliver(after=["s1"]))
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert "s1" in executed_ids(s, inp.task_id), "写高更保守，照常执行"


async def test_an_unregistered_template_is_denied(harness: StageHarness) -> None:
    s = harness.script
    registered(s)
    one_round(harness, atomic("s1", template="ghost@v1"), deliver())
    inp, _ = await harness.run()
    s1 = review_result(s, inp.task_id, "s1")
    assert s1.failure_class == "denied" and "template_not_registered" in s1.error
    assert "s1" not in executed_ids(s, inp.task_id)


async def test_an_unavailable_catalog_denies_the_whole_stage(harness: StageHarness) -> None:
    s = harness.script
    registered(s)
    s.catalog_error = "Registry 里没有模板 step@v1"
    one_round(harness, atomic("s1"), deliver(after=["s1"]))
    inp, result = await harness.run()
    assert executed_ids(s, inp.task_id) == [], "Catalog 取不到时一个 Node 也不执行"
    for node_id in ("s1", "answer"):
        r = review_result(s, inp.task_id, node_id)
        assert r.failure_class == "denied" and "catalog_unavailable" in r.error
    assert result.status == enums_pb2.TASK_STATUS_ABORTED


async def test_the_stub_catalog_is_not_enforced(harness: StageHarness) -> None:
    s = harness.script
    s.templates = {"send@v1": TemplateInfo(executor="program", effect="write_irreversible")}
    one_round(harness, atomic("send", template="send@v1", effect="none", role="transform"), deliver(after=["send"]))
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE, "没接 Registry 时不核对（端到端测试的默认不变）"
    assert "send" in executed_ids(s, inp.task_id)


# --- 审批不可绕过 ---


async def test_irreversible_approval_is_not_bypassed_by_hooks(harness: StageHarness) -> None:
    s = harness.script
    use_engine(s)
    s.policy = StagePolicy(approval_timeout_s=1)
    one_round(harness, atomic("pay", effect="write_irreversible", template="card@v1"), deliver())
    inp, _ = await harness.run(effect_ceiling=WRITE_CEILING)
    assert [q.question_id for q in s.questions] == ["pay.1"], "Hook 全部 allow，审批仍要问人"
    pay = review_result(s, inp.task_id, "pay")
    assert pay.failure_class == "denied", "没人批准时按拒绝处理"
    assert "pay" not in executed_ids(s, inp.task_id)


# --- before_finish ---


def finish_policy(s: Script) -> None:
    use_engine(s)
    s.policy = StagePolicy(hooks={"before_finish": ["acceptance_recheck"]}, human_timeout_s=30)


def answer_round(seq: int) -> dict[str, Any]:
    return stage(seq, deliver(output_schema=ANSWER_SCHEMA))


async def test_a_rejected_finish_goes_back_to_the_planner(harness: StageHarness) -> None:
    s = harness.script
    finish_policy(s)
    s.plans["main"] = [answer_round(1), answer_round(2)]
    s.behaviors["answer"] = sequence({"text": ""}, {"text": "退款率上升是因为物流延误"})
    s.reviewer = scripted(out("done", done={"deliver_node": "answer"}), out("done", done={"deliver_node": "answer"}))
    _, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert len(calls_at(s, "before_finish")) == 2
    assert len(s.plan_inputs) == 2, "驳回完成后回到 Planner"
    assert "acceptance_recheck" in s.plan_inputs[1].last_round["finish_rejected"], "下一轮的 Planner 看得到驳回的理由"
    assert deliverable(result)["text"] == "退款率上升是因为物流延误"


@pytest.mark.parametrize(("option", "expected"), [("continue_as_is", enums_pb2.TASK_STATUS_DONE), ("stop_and_wrap_up", enums_pb2.TASK_STATUS_ABORTED)])
async def test_finish_rejections_are_capped_then_a_human_decides(harness: StageHarness, option: str, expected: int) -> None:
    s = harness.script
    finish_policy(s)
    s.plans["main"] = [answer_round(1), answer_round(2), answer_round(3)]
    s.behaviors["answer"] = returns({"text": ""})
    s.reviewer = scripted(*(out("done", done={"deliver_node": "answer"}) for _ in range(3)))
    s.on_question = reply_with(harness, option)
    _, result = await harness.run()
    assert len(calls_at(s, "before_finish")) == 3
    assert len(s.plan_inputs) == 3, "驳回 2 次之后不再回到 Planner"
    (question,) = s.questions
    assert set(question.options) == set(HUMAN_REPLIES)
    assert result.status == expected


# --- Task 级挂载点 ---


async def test_task_level_mounts_run_in_order(harness: StageHarness) -> None:
    s = harness.script
    s.policy = StagePolicy(hooks={"on_task_received": ["content_filter"], "on_task_start": ["project_rules"], "on_task_end": ["cleanup"]})
    one_round(harness, deliver())
    _, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    mounts = [c.mount for c in s.hook_calls]
    task_mounts = [m for m in mounts if m.startswith("on_task")]
    assert task_mounts == ["on_task_received", "on_task_start", "on_task_end"]
    assert mounts.index("on_task_start") < mounts.index("before_node") < mounts.index("on_task_end"), "接收与开始在规划之前，结束在最后"
    assert len(s.plan_inputs) == 1


async def test_a_denied_task_received_hook_aborts_before_planning(harness: StageHarness) -> None:
    s = harness.script
    s.policy = StagePolicy(hooks={"on_task_received": ["content_filter"]})
    s.hook_overrides[("on_task_received", "*")] = {"decision": "deny", "reason": "消息里有违禁内容"}
    one_round(harness, deliver())
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_ABORTED and result.final_verdict == enums_pb2.VERDICT_ABORT
    assert not s.plan_inputs, "被拒的 Task 不规划"
    (received,) = calls_at(s, "on_task_received")
    assert received.subject["message"] == inp.message


async def test_a_denied_task_start_hook_aborts_before_planning(harness: StageHarness) -> None:
    s = harness.script
    s.policy = StagePolicy(hooks={"on_task_start": ["project_rules"]})
    s.hook_overrides[("on_task_start", "*")] = {"decision": "deny", "reason": "项目规则不允许"}
    one_round(harness, deliver())
    _, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_ABORTED and not s.plan_inputs
    (start,) = calls_at(s, "on_task_start")
    assert "task_state" in start.subject


@pytest.mark.parametrize(
    "override",
    [
        {"decision": "modify", "modification": {"target": "input", "patch": [{"op": "replace", "path": "/message", "value": "改写"}]}},
        {"decision": "inject", "injection": {"zone": "brief", "content": "补充上下文"}},
    ],
)
async def test_modify_and_inject_at_task_received_are_treated_as_deny(harness: StageHarness, override: dict[str, Any]) -> None:
    s = harness.script
    s.policy = StagePolicy(hooks={"on_task_received": ["content_filter"]})
    s.hook_overrides[("on_task_received", "*")] = override
    one_round(harness, deliver())
    _, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_ABORTED and not s.plan_inputs, "第一期在这个挂载点只认 allow 与 deny（fail-closed）"


async def test_task_end_hooks_are_recorded_but_do_not_change_the_outcome(harness: StageHarness) -> None:
    s = harness.script
    s.policy = StagePolicy(hooks={"on_task_end": ["cleanup"]})
    s.hook_overrides[("on_task_end", "*")] = {"decision": "deny", "reason": "清理失败"}
    one_round(harness, deliver())
    _, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    (end,) = calls_at(s, "on_task_end")
    assert end.subject["status"] == "TASK_STATUS_DONE"


async def test_hooks_declared_at_before_compact_fail_closed(harness: StageHarness) -> None:
    s = harness.script
    s.policy = StagePolicy(hooks={"before_compact": ["save_state"]})
    one_round(harness, deliver())
    _, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_ABORTED and not s.plan_inputs, "第一期没接 before_compact：声明了就拒绝，不悄悄跳过"


async def test_an_unavailable_tenant_policy_fails_closed(harness: StageHarness) -> None:
    s = harness.script
    s.policy_error = "固定的租户策略 v5 取不到"
    one_round(harness, deliver())
    _, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_ABORTED, "租户策略取不到一律按拒绝处理（设计书 3.1.2）"
    assert not s.plan_inputs and not s.calls


async def test_the_stage_policy_is_loaded_once_before_planning(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, atomic("q", role="fetch", effect="read")), stage(2, deliver())]
    s.deliver["main"] = "answer"
    await harness.run()
    assert len(s.policy_requests) == 1, "每个 Task 加载一次，各 Stage 共用"


async def test_executor_never_sees_a_denied_node(harness: StageHarness) -> None:
    """兜底：被 Hook 拒绝的 Node 不会交给执行器（执行器收到的都在 before_node 之后）。"""
    s = harness.script
    s.hook_overrides[("before_node", "q")] = {"decision": "deny", "reason": "权限不够"}
    seen: list[str] = []

    async def record(inp: NodeInput, _: Script) -> dict[str, Any]:
        seen.append(inp.node["id"])
        if inp.node["id"] == "q":
            raise ApplicationError("不该执行", type="should_not_run", non_retryable=True)
        return {"result": {"text": "ok"}}

    s.behaviors["q"] = record
    one_round(harness, atomic("q", role="fetch", effect="read"), deliver())
    await harness.run()
    assert "q" not in seen
