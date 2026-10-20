"""S-07 场景 2：Worker 在写操作已执行、结果未写入历史时崩溃。

设计书 3.6“恢复”、附录 K.4：写操作先用幂等键查询外部状态，确认未生效再执行；“Node 已完成，但结果未写入历史”
靠幂等键去重。设计书 1.1“可恢复”的验收：杀掉 Worker 后 Task 自动完成，写操作不重复生效。

故障 Worker 的 ledger 工具在外部写入之后（或之前）让进程立即退出（故障计划的 crash），再拉起一个新的 Worker。
外部 API 的三种能力各测一遍（e2e_support.fault_worker.LEDGER_TOOLS）：
- ledger_post：按 Idempotency-Key 去重 → 重试时幂等键必须不变（不含 attempt）；
- ledger_append：只支持按幂等键查询 → 重试前必须先查，查到就不再执行；
- ledger_transfer：write_irreversible、支持查询 → 不自动重试，超时后走对账；审批不因重启重问。
"""

from __future__ import annotations

import pytest

from e2e_support import history as h
from e2e_support.api import TaskApi
from e2e_support.faults import CRASH_EXIT_CODE, Rule
from e2e_support.scenarios import answer_node, fault_stage, human_asks, outcome, reviewer_saw, unique, wait_human_ask, write_node
from e2e_support.stack import Stack

pytestmark = [pytest.mark.e2e, pytest.mark.faults, pytest.mark.scenario(2)]


def crash_and_recover(stack: Stack, rule: Rule) -> None:
    """等 Worker 按规则退出，再拉起新的 Worker。"""
    assert stack.wait_worker_exit() == CRASH_EXIT_CODE, "Worker 应按故障计划退出"
    assert stack.faults.fired(rule.id) == 1
    stack.start_worker()


@pytest.mark.parametrize("tool", ["ledger_post", "ledger_append"])
def test_crash_after_reversible_write_applies_it_exactly_once(fstack: Stack, writer: TaskApi, tool: str) -> None:
    ref = unique("pay")
    rule = Rule(id="crash-after-apply", point="tool.after_apply", action="crash", match=(tool, ref), times=1)
    fstack.faults.set(rule)
    sub = writer.submit(inputs=fault_stage(write_node("pay", tool, ref, irreversible=False), answer_node(after=("pay",))))
    crash_and_recover(fstack, rule)

    assert outcome(writer, sub)["status"] == "TASK_STATUS_DONE"
    assert fstack.ledger.applied(ref) == 1, "外部写操作只生效一次"
    assert fstack.count(sub["taskId"], "execute_model_node", "end") == 1

    task = h.fetch_one(fstack.temporal_address, sub["taskId"])
    report = h.analyze(task)
    h.assert_clean(report, worker_restarted=True)
    pay = report.activity("execute_program_node", "pay")
    assert pay.outcome == "completed" and pay.attempts == 2, "被打断的 attempt 在同一次调度里重试"
    # 结果来自第一次（也是唯一一次）真正生效的写入。
    assert pay.result["result"]["txn_id"] == fstack.ledger.entries(ref)[0]["result"]["txn_id"]
    h.assert_replays_python([task])


def test_crash_before_reversible_write_executes_it_once_after_recovery(fstack: Stack, writer: TaskApi) -> None:
    ref = unique("pay")
    rule = Rule(id="crash-before-apply", point="tool.before_apply", action="crash", match=("ledger_append", ref), times=1)
    fstack.faults.set(rule)
    sub = writer.submit(inputs=fault_stage(write_node("pay", "ledger_append", ref, irreversible=False), answer_node(after=("pay",))))
    crash_and_recover(fstack, rule)

    assert outcome(writer, sub)["status"] == "TASK_STATUS_DONE"
    assert fstack.ledger.applied(ref) == 1, "查到外部没有生效，才执行；执行一次"
    task = h.fetch_one(fstack.temporal_address, sub["taskId"])
    h.assert_clean(h.analyze(task), worker_restarted=True)
    h.assert_replays_python([task])


def test_crash_after_irreversible_write_is_reconciled_not_repeated(fstack: Stack, writer: TaskApi) -> None:
    ref = unique("wire")
    rule = Rule(id="crash-after-apply", point="tool.after_apply", action="crash", match=("ledger_transfer", ref), times=1)
    fstack.faults.set(rule)
    sub = writer.submit(inputs=fault_stage(write_node("pay", "ledger_transfer", ref, irreversible=True), answer_node(after=("pay",))))
    task_id = sub["taskId"]
    wait_human_ask(fstack, task_id, "pay.1")
    writer.reply(task_id, "pay.1", "approve")
    crash_and_recover(fstack, rule)

    assert outcome(writer, sub)["status"] == "TASK_STATUS_DONE", "对账查到外部已生效，按成功继续"
    assert fstack.ledger.applied(ref) == 1, "不可逆写操作不重复执行"
    assert human_asks(fstack, task_id, "pay.1") == 1, "审批不因 Worker 重启而重问"

    task = h.fetch_one(fstack.temporal_address, task_id)
    report = h.analyze(task)
    # 不可逆写操作不自动重试：被打断的那次 activity 以超时结束，随后按幂等键对账一次。
    h.assert_clean(report, worker_restarted=True, allowed_failures=[("execute_program_node", "pay")])
    assert report.activity("execute_program_node", "pay").attempts == 1
    reconcile = report.activity("reconcile_node", "pay")
    assert reconcile.outcome == "completed" and reconcile.result["found"] is True
    h.assert_replays_python([task])


def test_crash_before_irreversible_write_is_not_retried(fstack: Stack, writer: TaskApi) -> None:
    ref = unique("wire")
    rule = Rule(id="crash-before-apply", point="tool.before_apply", action="crash", match=("ledger_transfer", ref), times=1)
    fstack.faults.set(rule)
    sub = writer.submit(inputs=fault_stage(write_node("pay", "ledger_transfer", ref, irreversible=True), answer_node(after=("pay",))))
    task_id = sub["taskId"]
    wait_human_ask(fstack, task_id, "pay.1")
    writer.reply(task_id, "pay.1", "approve")
    crash_and_recover(fstack, rule)

    # 对账确认没有生效；不可逆写操作不自动重试，交给 Reviewer（桩 Reviewer 判 abort）。
    assert outcome(writer, sub)["status"] == "TASK_STATUS_ABORTED"
    assert fstack.ledger.applied(ref) == 0
    assert reviewer_saw(fstack, task_id)["statuses"]["pay"] in ("unknown", "failed")
    assert fstack.count(task_id, "execute_model_node", "start") == 0, "下游节点不执行"

    task = h.fetch_one(fstack.temporal_address, task_id)
    h.assert_clean(h.analyze(task), worker_restarted=True, allowed_failures=[("execute_program_node", "pay")])
    h.assert_replays_python([task])
