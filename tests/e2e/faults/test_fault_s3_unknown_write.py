"""S-07 场景 3：写操作结果未知——外部已生效，响应丢失。

附录 K.4：结果记为 UNKNOWN，走对账路径确认，不当作失败直接重试。CLAUDE.md：写操作超时且结果未知时，
先按幂等键对账，不直接重试；write_irreversible 不自动重试。

故障 Worker 的 ledger 工具先在外部写入，再让响应晚于工具超时回来（故障计划的 lose_response），调用方只看到超时。
"""

from __future__ import annotations

import pytest

from e2e_support import history as h
from e2e_support.api import TaskApi
from e2e_support.faults import Rule
from e2e_support.scenarios import answer_node, fault_stage, outcome, reviewer_saw, unique, wait_human_ask, write_node
from e2e_support.stack import Stack

pytestmark = [pytest.mark.e2e, pytest.mark.faults, pytest.mark.scenario(3)]


def submit_write(stack: Stack, api: TaskApi, tool: str, ref: str, *, irreversible: bool) -> dict:
    sub = api.submit(inputs=fault_stage(write_node("pay", tool, ref, irreversible=irreversible), answer_node(after=("pay",))))
    if irreversible:
        wait_human_ask(stack, sub["taskId"], "pay.1")
        api.reply(sub["taskId"], "pay.1", "approve")
    return sub


def test_lost_response_of_a_lookup_capable_write_is_reconciled(fstack: Stack, writer: TaskApi) -> None:
    ref = unique("wire")
    fstack.faults.set(Rule(id="lost", point="tool.after_apply", action="lose_response", match=("ledger_transfer", ref), times=1))
    sub = submit_write(fstack, writer, "ledger_transfer", ref, irreversible=True)

    assert outcome(writer, sub)["status"] == "TASK_STATUS_DONE", "按幂等键查到外部已生效，按成功继续"
    assert fstack.ledger.applied(ref) == 1, "对账，不重试"

    task = h.fetch_one(fstack.temporal_address, sub["taskId"])
    report = h.analyze(task)
    h.assert_clean(report)
    pay = report.activity("execute_program_node", "pay")
    assert pay.outcome == "completed" and pay.attempts == 1
    assert pay.result["result"]["txn_id"] == fstack.ledger.entries(ref)[0]["result"]["txn_id"], "结果取自外部的生效记录"
    h.assert_replays_python([task])


def test_lost_response_of_an_idempotent_write_retries_with_the_same_key(fstack: Stack, writer: TaskApi) -> None:
    ref = unique("post")
    fstack.faults.set(Rule(id="lost", point="tool.after_apply", action="lose_response", match=("ledger_post", ref), times=1))
    sub = submit_write(fstack, writer, "ledger_post", ref, irreversible=False)

    assert outcome(writer, sub)["status"] == "TASK_STATUS_DONE"
    entries = fstack.ledger.entries(ref)
    assert len(entries) == 1, "外部按 Idempotency-Key 去重：重试用同一个幂等键，只生效一次"

    task = h.fetch_one(fstack.temporal_address, sub["taskId"])
    report = h.analyze(task)
    h.assert_clean(report)
    pay = report.activity("execute_program_node", "pay")
    assert pay.attempts == 2, "不支持查询的可逆写操作：带同一个幂等键重试"
    h.assert_replays_python([task])


def test_lost_response_without_lookup_is_unknown_and_not_retried(fstack: Stack, writer: TaskApi) -> None:
    ref = unique("wire")
    fstack.faults.set(Rule(id="lost", point="tool.after_apply", action="lose_response", match=("ledger_wire", ref), times=1))
    sub = submit_write(fstack, writer, "ledger_wire", ref, irreversible=True)
    task_id = sub["taskId"]

    # 没法确认：Node 状态是 unknown，交给 Reviewer（桩 Reviewer 判 abort，等人工确认）。
    assert outcome(writer, sub)["status"] == "TASK_STATUS_ABORTED"
    assert fstack.ledger.applied(ref) == 1, "结果未知的不可逆写操作不重试"
    assert reviewer_saw(fstack, task_id)["statuses"]["pay"] == "unknown"
    assert fstack.count(task_id, "execute_model_node", "start") == 0

    task = h.fetch_one(fstack.temporal_address, task_id)
    report = h.analyze(task)
    h.assert_clean(report, allowed_failures=[("execute_program_node", "pay")])
    assert report.activity("execute_program_node", "pay").attempts == 1
    h.assert_replays_python([task])


def test_reconcile_keeps_trying_while_the_lookup_is_temporarily_unavailable(fstack: Stack, writer: TaskApi) -> None:
    ref = unique("wire")
    fstack.faults.set(
        Rule(id="lost", point="tool.after_apply", action="lose_response", match=("ledger_transfer", ref), times=1),
        # 对账用的查询接口也暂时不可用：前两次查询报 transient。
        Rule(id="lookup-down", point="tool.lookup", action="transient", match=("ledger_transfer",), times=2),
    )
    sub = submit_write(fstack, writer, "ledger_transfer", ref, irreversible=True)

    assert outcome(writer, sub)["status"] == "TASK_STATUS_DONE", "查询恢复后对账成功，不把暂时查不了当作失败"
    assert fstack.ledger.applied(ref) == 1
    assert fstack.faults.fired("lookup-down") == 2

    task = h.fetch_one(fstack.temporal_address, sub["taskId"])
    report = h.analyze(task)
    h.assert_clean(report, allowed_failures=[("execute_program_node", "pay")])
    h.assert_replays_python([task])
