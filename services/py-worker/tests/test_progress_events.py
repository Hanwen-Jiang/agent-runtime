"""T-11b 验收：进度事件推流（设计书 4.14、附录 K.1；R-05 评审 D10；决定见 T-11b 的回复）。

Task 工作流经跨语言 activity publish_task_event（Session 队列，Go 的 Session Worker 执行；这里是脚本的替身）推送进度事件：
- 每个 Round：round_started → stage_started（节点清单）→ 每个顶层 Node 结束时一个 node_finished → Reviewer 判定后一个 verdict；
- 发出人工问题之后、开始等回复之前一个 human_input_required，question_id 就是 SignalTask 要带的那个；
- 载荷带 event（与 SSE 的事件名相同），符合 schemas/task_event_payload.schema.json；不带 Node 的结果与 rationale；
- 去重键在一个 Task 里唯一，重放时不变；
- 推送失败不影响 Task：这一 Round 余下的事件不再推，下一 Round 照常；
- 只有 Task 工作流推，sub_agent 等子工作流不推。

2026-10-28 人审通过的补充（版本分支 PROGRESS_DETAIL_PATCH 之后的运行）：
- 顶层 Node 开始执行时一个 node_started（因上游没成功而跳过的不推）；
- 顶层 Goal Node 的 tool_loop 里每次工具调用：调用前 tool_status running，调用后 succeeded 或 failed（带用时；新建了沙箱环境
  的带冷启动时间）。target 是简短、经凭据脱敏的说明，不带工具的输出。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

from agentruntime.common.v1 import enums_pb2
from agentruntime.workflow.v1 import task_pb2

from agentruntime_worker import names
from agentruntime_worker.activities.types import ReviewRoundOutput, StagePolicy, ToolLoopStep

from .stage_harness import StageHarness, fails, harness, returns  # noqa: F401 — harness 是 fixture
from .support import assert_valid
from .test_stage_runner import atomic, deliver, goal, one_round, scripted_steps, stage

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

IRREVERSIBLE = enums_pb2.EFFECT_WRITE_IRREVERSIBLE
HUMAN_REPLIES = ["continue_as_is", "replan_with_guidance", "stop_and_wrap_up"]


def progress(h: StageHarness, task_id: str) -> list[dict[str, Any]]:
    """这个 Task 推送成功的进度事件（载荷），每个都要符合契约里的 schema。"""
    events = h.script.progress(task_id)
    for payload in events:
        assert_valid("task_event_payload", payload)
    return events


def kinds(events: list[dict[str, Any]]) -> list[str]:
    return [e["event"] for e in events]


def keys_of(h: StageHarness, task_id: str) -> list[str]:
    return [k for k, e, _ in h.script.published if e.task_id == task_id]


async def wait_for_event(h: StageHarness, task_id: str, event: str) -> dict[str, Any]:
    await h.wait_for(lambda: any(e["event"] == event for e in h.script.progress(task_id)), f"进度事件 {event}")
    return next(e for e in progress(h, task_id) if e["event"] == event)


def reply(question_id: str, option: str, guidance: str = "") -> task_pb2.HumanReplySignal:
    return task_pb2.HumanReplySignal(question_id=question_id, option=option, guidance=guidance, replied_by_user_id="u1")


# --- 一个 Round 的事件 ---


async def test_a_round_publishes_round_stage_nodes_and_verdict(harness: StageHarness) -> None:
    s = harness.script
    s.behaviors["fetch_rates"] = returns({"rows": ["SECRET-RESULT-ROW"]})
    one_round(harness, atomic("fetch_rates", effect="read", role="fetch"), deliver(after=["fetch_rates"]))
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE

    events = progress(harness, inp.task_id)
    assert kinds(events) == ["round_started", "stage_started", "node_started", "node_finished", "node_started", "node_finished", "verdict"]
    assert events[0] == {"event": "round_started", "round_seq": 1, "rerun": False}
    started = events[1]
    assert started["round_seq"] == 1 and started["triage_level"] == "full" and started["goal"] == "第 1 轮" and started["exit_when"] == "完成"
    assert started["nodes"] == [
        {"id": "fetch_rates", "kind": "atomic", "role": "fetch"},
        {"id": "answer", "kind": "atomic", "role": "deliver"},
    ], "节点清单按 Stage 里的顺序，只有 id、kind 与 role"
    assert events[2] == {"event": "node_started", "round_seq": 1, "node_id": "fetch_rates", "kind": "atomic", "role": "fetch", "executor": "program"}
    assert [(e["node_id"], e["status"], e["role"]) for e in (events[3], events[5])] == [("fetch_rates", "succeeded", "fetch"), ("answer", "succeeded", "deliver")]
    assert events[6]["verdict"] == "done" and events[6]["round_seq"] == 1 and events[6]["reason"]

    text = json.dumps(events, ensure_ascii=False)
    assert "SECRET-RESULT-ROW" not in text and "RATIONALE" not in text, "进度事件不带 Node 的结果与 rationale"
    keys = keys_of(harness, inp.task_id)
    assert len(keys) == len(set(keys)) == 7, f"去重键在一个 Task 里唯一：{keys}"
    assert {w for _, e, w in harness.script.published if e.task_id == inp.task_id} == {inp.task_id}, "由 Task 工作流推送"


async def test_every_round_starts_and_ends_with_its_own_events(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, atomic("look", effect="read", role="fetch")), stage(2, deliver())]
    s.deliver["main"] = "answer"
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    events = progress(harness, inp.task_id)
    assert kinds(events) == ["round_started", "stage_started", "node_started", "node_finished", "verdict"] * 2
    assert [e["round_seq"] for e in events] == [1] * 5 + [2] * 5
    assert [e["verdict"] for e in events if e["event"] == "verdict"] == ["continue", "done"]


async def test_failed_and_skipped_nodes_are_reported_with_their_status(harness: StageHarness) -> None:
    s = harness.script
    s.behaviors["fetch_rates"] = fails("数据源 503")
    one_round(harness, atomic("fetch_rates", effect="read", role="fetch"), deliver(after=["fetch_rates"]))
    inp, _ = await harness.run()
    events = progress(harness, inp.task_id)
    finished = {e["node_id"]: e for e in events if e["event"] == "node_finished"}
    assert finished["fetch_rates"]["status"] == "failed" and "数据源 503" in finished["fetch_rates"]["error"]
    assert finished["answer"]["status"] == "skipped" and finished["answer"]["failure_class"] == "upstream_failed"
    assert all(len(e.get("error", "")) <= 300 for e in finished.values()), "错误说明截短"
    assert [e["node_id"] for e in events if e["event"] == "node_started"] == ["fetch_rates"], "跳过的 Node 没有开始，不推 node_started"


async def test_parallel_nodes_are_each_reported_once(harness: StageHarness) -> None:
    one_round(
        harness,
        atomic("q_a", effect="read", role="fetch"),
        atomic("q_b", effect="read", role="fetch"),
        atomic("q_c", effect="read", role="fetch"),
        deliver(after=["q_a", "q_b", "q_c"]),
    )
    inp, _ = await harness.run()
    events = progress(harness, inp.task_id)
    finished = [e["node_id"] for e in events if e["event"] == "node_finished"]
    assert sorted(finished) == ["answer", "q_a", "q_b", "q_c"] and finished[-1] == "answer", "每个顶层 Node 一个，按结束的先后"
    assert [e["node_id"] for e in events if e["event"] == "node_started"] == ["q_a", "q_b", "q_c", "answer"], "同时就绪的按 id 排序开始"
    assert kinds(events)[-1] == "verdict"


async def test_child_workflows_do_not_publish(harness: StageHarness) -> None:
    s = harness.script
    s.tools.update({"web_search": "read"})
    s.tool_steps["research"] = scripted_steps(ToolLoopStep(action="tool", tool="web_search", args={"q": "退款率"}), ToolLoopStep(action="finish", result={"ok": True}))
    one_round(harness, goal("research", "sub_agent", tools=["web_search"]), deliver(after=["research"]))
    inp, _ = await harness.run()
    publishers = {w for _, e, w in s.published if e.task_id == inp.task_id}
    assert publishers == {inp.task_id}, f"只有 Task 工作流推送，子工作流不推：{publishers}"
    events = progress(harness, inp.task_id)
    assert [e["node_id"] for e in events if e["event"] == "node_finished"] == ["research", "answer"]
    assert "tool_status" not in kinds(events), "sub_agent 在子工作流里调用工具，不推 tool_status"


# --- tool_loop 里每次工具调用（node_started、tool_status） ---


def tool_statuses(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e["event"] == "tool_status"]


async def test_a_tool_loop_reports_every_tool_call(harness: StageHarness) -> None:
    s = harness.script
    s.tools.update({"repo_read": "read", "shell_run": "read"})
    s.tool_steps["fix"] = scripted_steps(
        ToolLoopStep(action="tool", tool="repo_read", args={"path": "src/pricing.py"}),
        ToolLoopStep(action="tool", tool="shell_run", args={"argv": ["python", "-m", "unittest"]}),
        ToolLoopStep(action="finish", result={"ok": True}),
    )
    s.tool_results["repo_read"] = lambda i: {"content": "SECRET-FILE-CONTENT"}
    s.tool_results["shell_run"] = lambda i: {"exit_code": 0, "stdout": "SECRET-TEST-OUTPUT", "sandbox": "gvisor", "sandbox_startup_ms": 8100}
    one_round(harness, goal("fix", "tool_loop", tools=["repo_read", "shell_run"]), deliver(after=["fix"]))
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE

    events = progress(harness, inp.task_id)
    assert kinds(events) == [
        "round_started", "stage_started", "node_started",
        "tool_status", "tool_status", "tool_status", "tool_status",
        "node_finished", "node_started", "node_finished", "verdict",
    ]
    assert events[2] == {"event": "node_started", "round_seq": 1, "node_id": "fix", "kind": "goal"}
    calls = tool_statuses(events)
    assert [(c["step"], c["tool"], c["state"]) for c in calls] == [
        (1, "repo_read", "running"), (1, "repo_read", "succeeded"), (2, "shell_run", "running"), (2, "shell_run", "succeeded"),
    ]
    assert all(c["node_id"] == "fix" and c["round_seq"] == 1 for c in calls)
    assert [c.get("target") for c in calls] == ["src/pricing.py"] * 2 + ["python -m unittest"] * 2
    # 载荷经 protobuf 的 Struct：数字读回来是浮点数（schema 里按整数核对，1.0 也算整数）。
    assert "duration_ms" not in calls[0] and float(calls[1]["duration_ms"]).is_integer() and calls[1]["duration_ms"] >= 0
    assert "sandbox_startup_ms" not in calls[1], "repo_read 没有新建沙箱环境"
    assert calls[3]["sandbox_startup_ms"] == 8100, "新建了沙箱环境时带冷启动时间"
    text = json.dumps(events, ensure_ascii=False)
    assert "SECRET-FILE-CONTENT" not in text and "SECRET-TEST-OUTPUT" not in text, "进度事件不带工具的输出"
    keys = keys_of(harness, inp.task_id)
    assert len(keys) == len(set(keys)), f"去重键在一个 Task 里唯一：{keys}"


async def test_a_failed_tool_call_is_reported_and_the_loop_goes_on(harness: StageHarness) -> None:
    s = harness.script
    s.tools.update({"repo_read": "read"})
    s.tool_steps["fix"] = scripted_steps(
        ToolLoopStep(action="tool", tool="repo_read", args={"path": "nope.py"}),
        ToolLoopStep(action="finish", result={"ok": True}),
    )
    s.tool_results["repo_read"] = lambda i: RuntimeError("文件不存在：" + "x" * 400)
    one_round(harness, goal("fix", "tool_loop", tools=["repo_read"]), deliver(after=["fix"]))
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    calls = tool_statuses(progress(harness, inp.task_id))
    assert [c["state"] for c in calls] == ["running", "failed"]
    assert "文件不存在" in calls[1]["error"] and len(calls[1]["error"]) <= 300 and calls[1]["duration_ms"] >= 0


async def test_a_tool_outside_the_node_is_not_called_and_not_reported_as_running(harness: StageHarness) -> None:
    s = harness.script
    s.tools.update({"repo_read": "read"})
    s.tool_steps["fix"] = scripted_steps(
        ToolLoopStep(action="tool", tool="rm_rf", args={"path": "/"}),
        ToolLoopStep(action="finish", result={"ok": True}),
    )
    one_round(harness, goal("fix", "tool_loop", tools=["repo_read"]), deliver(after=["fix"]))
    inp, _ = await harness.run()
    assert tool_statuses(progress(harness, inp.task_id)) == [], "不在可用列表里的工具不调用，也就没有调用的进度"


async def test_tool_targets_are_short_and_carry_no_credentials(harness: StageHarness) -> None:
    s = harness.script
    s.tools.update({"shell_run": "read", "web_fetch": "read"})
    command = "curl -H 'Authorization: Bearer sk-live-0123456789abcdef0123456789' https://bot:hunter2@git.example.com/a.git && " + "echo x " * 60
    s.tool_steps["fix"] = scripted_steps(
        ToolLoopStep(action="tool", tool="shell_run", args={"command": command}),
        ToolLoopStep(action="tool", tool="shell_run", args={"argv": ["python", "-m", "pytest", "-q"], "env": {"API_KEY": "abc"}}),
        ToolLoopStep(action="tool", tool="web_fetch", args={"url": "https://docs.example.com/guide?token=ghp_0123456789abcdef0123456789abcdef0123"}),
        ToolLoopStep(action="finish", result={"ok": True}),
    )
    one_round(harness, goal("fix", "tool_loop", tools=["shell_run", "web_fetch"]), deliver(after=["fix"]))
    inp, _ = await harness.run()
    running = [c for c in tool_statuses(progress(harness, inp.task_id)) if c["state"] == "running"]
    assert len(running) == 3
    first, second, third = (c["target"] for c in running)
    assert len(first) <= 160 and first.startswith("curl"), first
    for secret in ("sk-live-0123456789abcdef0123456789", "hunter2", "ghp_0123456789abcdef0123456789abcdef0123"):
        assert secret not in first + second + third, f"target 里不能有凭据：{secret}"
    assert second == "python -m pytest -q" and "abc" not in second, "shell_run 的 argv 按空格连起来，环境变量不进 target"
    assert third.startswith("https://docs.example.com/guide")


# --- 等待人工回复 ---


async def test_a_human_node_question_is_published_before_waiting(harness: StageHarness) -> None:
    confirm = atomic("confirm", executor="human", role="gate", template="confirm_scope@v1", inputs={"question": "按华东的范围继续吗？", "options": ["approve", "reject"]})
    one_round(harness, confirm, deliver(after=["confirm"]))
    inp, handle = await harness.start()
    asked = await wait_for_event(harness, inp.task_id, "human_input_required")
    assert asked["question_id"] == "confirm" and asked["kind"] == "human" and asked["round_seq"] == 1
    assert asked["text"] == "按华东的范围继续吗？" and asked["options"] == ["approve", "reject"] and asked["deadline"]
    # 客户端拿事件里的 question_id 与选项回复（SignalTask），Task 接着跑完。
    await handle.signal(names.HUMAN_REPLY_SIGNAL, reply(asked["question_id"], asked["options"][0]))
    result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert kinds(progress(harness, inp.task_id)) == [
        "round_started", "stage_started", "node_started", "human_input_required", "node_finished", "node_started", "node_finished", "verdict",
    ]


async def test_an_irreversible_write_asks_for_approval(harness: StageHarness) -> None:
    one_round(harness, atomic("pay_ticket", effect="write_irreversible"), deliver(after=["pay_ticket"]))
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    asked = await wait_for_event(harness, inp.task_id, "human_input_required")
    assert (asked["question_id"], asked["kind"], asked["options"]) == ("pay_ticket.1", "approval", ["approve", "reject"])
    await handle.signal(names.HUMAN_REPLY_SIGNAL, reply("pay_ticket.1", "approve"))
    assert (await asyncio.wait_for(handle.result(), timeout=30)).status == enums_pb2.TASK_STATUS_DONE


async def test_an_irreversible_tool_call_in_a_loop_asks_for_approval(harness: StageHarness) -> None:
    s = harness.script
    s.tools.update({"card_charge": "write_irreversible"})
    s.tool_steps["book"] = scripted_steps(ToolLoopStep(action="tool", tool="card_charge", args={"flight": "MU5101"}), ToolLoopStep(action="finish", result={"ticket": "781"}))
    one_round(harness, goal("book", "tool_loop", tools=["card_charge"], effect_ceiling="write_irreversible"), deliver(after=["book"]))
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    asked = await wait_for_event(harness, inp.task_id, "human_input_required")
    assert (asked["question_id"], asked["kind"]) == ("book.2.1", "tool_approval") and "MU5101" in asked["text"]
    await handle.signal(names.HUMAN_REPLY_SIGNAL, reply("book.2.1", "approve"))
    assert (await asyncio.wait_for(handle.result(), timeout=30)).status == enums_pb2.TASK_STATUS_DONE


async def test_a_reviewer_escalation_asks_the_user(harness: StageHarness) -> None:
    s = harness.script
    escalate = {"verdict": "escalate", "reason": "最便宜的不可退改", "escalate": {"target": "human", "question": "订不可退改的那班吗？", "options": HUMAN_REPLIES, "state_summary": "已比价"}}
    reviews = [ReviewRoundOutput(verdict=escalate, state_update={})]

    async def reviewer(_: Any) -> ReviewRoundOutput:
        return reviews.pop(0)

    s.reviewer = reviewer
    one_round(harness, deliver())
    inp, handle = await harness.start()
    asked = await wait_for_event(harness, inp.task_id, "human_input_required")
    assert (asked["question_id"], asked["kind"], asked["options"]) == ("_review", "escalation", HUMAN_REPLIES)
    assert asked["text"] == "订不可退改的那班吗？"
    await handle.signal(names.HUMAN_REPLY_SIGNAL, reply("_review", "stop_and_wrap_up"))
    assert (await asyncio.wait_for(handle.result(), timeout=30)).status == enums_pb2.TASK_STATUS_ABORTED
    assert kinds(progress(harness, inp.task_id))[-2:] == ["verdict", "human_input_required"], "先推判定，再推问题"


# --- 推送失败 ---


async def test_a_failed_publish_does_not_fail_the_task_and_skips_the_rest_of_the_round(harness: StageHarness) -> None:
    s = harness.script
    s.plans["main"] = [stage(1, atomic("look", effect="read", role="fetch")), stage(2, deliver())]
    s.deliver["main"] = "answer"

    def broken_in_round_one(key: str, event: Any) -> None:
        if event.payload.value.struct_value.fields["round_seq"].number_value == 1:
            raise ApplicationError("Session Worker 不在", type="unavailable", non_retryable=True)

    s.on_publish = broken_in_round_one
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE, "推送失败不影响 Task"
    # 去重键形如 r<Round>/<事件>[/<Node 或问题>]（决定见回复）；这个替身只服务这一个 Task。
    assert [k for k in s.publish_attempts if k.startswith("r1/")] == ["r1/round_started"], "第 1 轮推送失败一次之后，这一轮余下的事件不再推"
    assert kinds(progress(harness, inp.task_id)) == ["round_started", "stage_started", "node_started", "node_finished", "verdict"], "第 2 轮照常推送"
    assert {e["round_seq"] for e in progress(harness, inp.task_id)} == {2}


async def test_publishing_retries_a_transient_failure(harness: StageHarness) -> None:
    s = harness.script
    one_round(harness, deliver())
    failed: set[str] = set()

    def flaky(key: str, event: Any) -> None:
        if key not in failed:
            failed.add(key)
            raise ApplicationError("连接被重置", type="transient")

    s.on_publish = flaky
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert kinds(progress(harness, inp.task_id)) == ["round_started", "stage_started", "node_started", "node_finished", "verdict"], "暂时性的失败重试后照常推送"


async def test_the_question_deadline_is_an_absolute_time(harness: StageHarness) -> None:
    """人工问题的 deadline 是绝对时间（ISO 8601，工作流时间），不是策略里的秒数。"""
    harness.script.policy = StagePolicy(human_timeout_s=90)
    confirm = atomic("confirm", executor="human", role="gate", template="confirm_scope@v1", inputs={"question": "继续吗？", "options": ["approve", "reject"]})
    one_round(harness, confirm, deliver(after=["confirm"]))
    inp, handle = await harness.start()
    asked = await wait_for_event(harness, inp.task_id, "human_input_required")
    assert "T" in asked["deadline"] and asked["deadline"].endswith(("Z", "+00:00"))
    await handle.signal(names.HUMAN_REPLY_SIGNAL, reply("confirm", "approve"))
    await asyncio.wait_for(handle.result(), timeout=30)
