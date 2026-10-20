"""S-07 场景 5：human 节点等待期间，Worker 全部重启。

设计书 3.6、附录 K.4：等待人工时工作流挂起，不占用 Worker；信号到达后继续。等待用持久化定时器，
Worker 不在时到期也照样生效。端到端环境只有一个 Worker 进程（订阅全部队列），杀掉它就是“全部重启”。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from temporalio.client import Client

from e2e_support import history as h
from e2e_support.api import TaskApi
from e2e_support.scenarios import answer_node, fault_stage, human_asks, human_cancels, human_node, outcome, unique, wait_human_ask, write_node
from e2e_support.stack import Stack

pytestmark = [pytest.mark.e2e, pytest.mark.faults, pytest.mark.scenario(5)]


async def _pending_activities(address: str, workflow_id: str) -> list[str]:
    client = await Client.connect(address)
    desc = await client.get_workflow_handle(workflow_id).describe()
    return [p.activity_type.name for p in desc.raw_description.pending_activities]


def pending_activities(stack: Stack, workflow_id: str) -> list[str]:
    return asyncio.run(_pending_activities(stack.temporal_address, workflow_id))


def test_reply_while_every_worker_is_down_resumes_the_task(fstack: Stack, api: TaskApi) -> None:
    sub = api.submit(inputs=fault_stage(human_node("confirm"), answer_node(after=("confirm",))))
    task_id = sub["taskId"]
    wait_human_ask(fstack, task_id, "confirm")
    api.wait_status(task_id, "TASK_STATUS_RUNNING")
    time.sleep(1)
    assert pending_activities(fstack, task_id) == [], "等人工时没有在跑的 activity，不占用 Worker"

    fstack.kill_worker()
    api.reply(task_id, "confirm", "approve", "Worker 不在时回复")  # 信号由 Temporal 记下，不需要 Worker
    time.sleep(1)
    fstack.start_worker()

    assert outcome(api, sub)["status"] == "TASK_STATUS_DONE"
    assert human_asks(fstack, task_id, "confirm") == 1, "重启后不重发问题"
    assert fstack.count(task_id, "plan_round", "end") == 1
    assert fstack.count(task_id, "execute_model_node", "end") == 1

    task = h.fetch_one(fstack.temporal_address, task_id)
    report = h.analyze(task)
    h.assert_clean(report, worker_restarted=True)
    assert report.signals.count("human_reply") == 1
    h.assert_replays_python([task])


def test_human_timeout_that_expires_while_every_worker_is_down_still_applies(fstack: Stack, api: TaskApi) -> None:
    timeout_s = 4
    sub = api.submit(inputs=fault_stage(human_node("confirm", timeout_s=timeout_s), answer_node(after=("confirm",))))
    task_id = sub["taskId"]
    wait_human_ask(fstack, task_id, "confirm")
    fstack.kill_worker()
    time.sleep(timeout_s + 3)  # 定时器在 Worker 不在时到期
    fstack.start_worker()

    # 超时按拒绝处理（附录 D.3），后面的节点不执行，桩 Reviewer 判 abort。
    assert outcome(api, sub)["status"] == "TASK_STATUS_ABORTED"
    assert fstack.count(task_id, "execute_model_node", "start") == 0
    assert human_asks(fstack, task_id, "confirm") == 1
    assert human_cancels(fstack, task_id, "confirm") == 1, "超时后撤回问题，只撤回一次"

    task = h.fetch_one(fstack.temporal_address, task_id)
    report = h.analyze(task)
    h.assert_clean(report, worker_restarted=True)
    assert report.timers_fired == 1
    h.assert_replays_python([task])


def test_restart_during_approval_neither_reasks_nor_skips_the_gate(fstack: Stack, writer: TaskApi) -> None:
    ref = unique("wire")
    sub = writer.submit(inputs=fault_stage(write_node("pay", "ledger_transfer", ref, irreversible=True), answer_node(after=("pay",))))
    task_id = sub["taskId"]
    wait_human_ask(fstack, task_id, "pay.1")
    fstack.kill_worker()
    time.sleep(1)
    fstack.start_worker()
    time.sleep(3)
    assert fstack.ledger.applied(ref) == 0, "审批没有回复之前，不可逆写操作不执行"

    writer.reply(task_id, "pay.1", "approve")
    assert outcome(writer, sub)["status"] == "TASK_STATUS_DONE"
    assert fstack.ledger.applied(ref) == 1
    assert human_asks(fstack, task_id, "pay.1") == 1

    task = h.fetch_one(fstack.temporal_address, task_id)
    h.assert_clean(h.analyze(task), worker_restarted=True)
    h.assert_replays_python([task])
