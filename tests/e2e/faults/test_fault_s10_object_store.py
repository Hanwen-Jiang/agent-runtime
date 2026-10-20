"""S-07 场景 10：ObjectStore 暂时不可用——Claim-Check 的重试与报错。

ObjectStore 在 Runtime 里有两处用途，都要经得起它暂时不可用：

- Node Executor 把超过 64 KB 的 Node 结果写进 ObjectStore，历史里只留摘要与引用（S-05）。ObjectStore 报 transient 时
  activity 重试；一直不可用时 Node 失败，错误分类为 transient，交给 Reviewer；写操作不因为重试而重复生效。
- Claim-Check（S-06）：超过阈值的 Temporal 载荷由 PayloadCodec 卸载到 ObjectStore。ObjectStore 暂时不可用时编解码器
  先在本地重试，用不着工作流任务失败；一直不可用时工作流任务失败、报错指明 ObjectStore 不可用（claim_check_unavailable），
  Temporal 不断重试工作流任务，ObjectStore 恢复后 Task 照常完成。

ObjectStore 的故障由故障 Worker 的 FaultyObjectStoreProvider 按故障计划注入（objects.put 报 transient，相当于 S3 返回 503）；
Worker 的 Claim-Check 也经过它。task-service（Go）一侧的不可用由 Go 的单元测试覆盖。
"""

from __future__ import annotations

import pytest

from e2e_support import history as h
from e2e_support.api import TaskApi
from e2e_support.faults import Rule
from e2e_support.scenarios import answer_node, fault_stage, outcome, reviewer_saw, unique, write_node
from e2e_support.stack import Stack, wait_until

pytestmark = [pytest.mark.e2e, pytest.mark.faults, pytest.mark.scenario(10)]

RESULT_KB = 100
# 历史里单个载荷的上限：大结果必须以引用出现（Node Executor 的内联上限与 Claim-Check 的阈值都是 64 KiB）。
MAX_INLINE_BYTES = 64 * 1024


def object_puts(stack: Stack, task_id: str, event: str = "put") -> list[dict]:
    return [c for c in stack.calls(task_id) if c["activity"] == "object_store" and c["event"] == event]


def test_large_result_waits_out_a_temporary_object_store_outage(fstack: Stack, api: TaskApi) -> None:
    node_id = unique("big")
    fstack.faults.set(Rule(id="objects-down", point="objects.put", action="transient", match=(f"/{node_id}-",), times=2))
    sub = api.submit(inputs=fault_stage(answer_node(node_id=node_id, result_kb=RESULT_KB, retry_max=3)))
    task_id = sub["taskId"]

    result = outcome(api, sub)
    assert result["status"] == "TASK_STATUS_DONE"
    deliverable = result["deliverable"]["value"]
    assert deliverable["ref"].startswith("obj://") and deliverable["size_bytes"] > RESULT_KB * 1024, "交付的是引用，不是 100 KB 的原文"

    assert len(object_puts(fstack, task_id, "put_failed")) == 2
    assert len({c["key"] for c in object_puts(fstack, task_id)}) == 1, "恢复后只写入一个对象"

    task = h.fetch_one(fstack.temporal_address, task_id)
    report = h.analyze(task)
    h.assert_clean(report)
    assert report.activity("execute_model_node", node_id).attempts == 3, "ObjectStore 报 transient 时 activity 重试"
    assert report.max_payload_bytes < MAX_INLINE_BYTES, f"历史里不该有大载荷，最大的有 {report.max_payload_bytes} 字节"
    h.assert_replays_python([task])


def test_outage_after_a_write_does_not_repeat_the_write(fstack: Stack, writer: TaskApi) -> None:
    ref = unique("post")
    # 写操作已生效，结果太大要写进 ObjectStore 时 ObjectStore 不可用：activity 重试，写操作不能再生效一次。
    fstack.faults.set(Rule(id="objects-down", point="objects.put", action="transient", match=("/pay-",), times=2))
    sub = writer.submit(inputs=fault_stage(write_node("pay", "ledger_append", ref, irreversible=False, echo_kb=RESULT_KB), answer_node(after=("pay",))))

    assert outcome(writer, sub)["status"] == "TASK_STATUS_DONE"
    assert fstack.ledger.applied(ref) == 1
    task = h.fetch_one(fstack.temporal_address, sub["taskId"])
    report = h.analyze(task)
    h.assert_clean(report)
    assert report.activity("execute_program_node", "pay").attempts == 3
    h.assert_replays_python([task])


def test_persistent_object_store_outage_fails_the_node_with_a_clear_error(fstack: Stack, api: TaskApi) -> None:
    node_id = unique("big")
    retry_max = 2
    fstack.faults.set(Rule(id="objects-down", point="objects.put", action="transient", match=(f"/{node_id}-",)))
    sub = api.submit(inputs=fault_stage(answer_node(node_id=node_id, result_kb=RESULT_KB, retry_max=retry_max)))
    task_id = sub["taskId"]

    assert outcome(api, sub)["status"] == "TASK_STATUS_ABORTED"
    saw = reviewer_saw(fstack, task_id)
    assert saw["statuses"][node_id] == "failed"
    assert saw["errors"][node_id]["failure_class"] == "transient", "依赖暂时不可用，分类为 transient，不是 internal"
    assert "unavailable" in saw["errors"][node_id]["error"], "错误信息说明是依赖不可用"
    assert len(object_puts(fstack, task_id, "put_failed")) == retry_max + 1, "重试有上限"

    task = h.fetch_one(fstack.temporal_address, task_id)
    report = h.analyze(task)
    h.assert_clean(report, allowed_failures=[("execute_model_node", node_id)])
    h.assert_replays_python([task])


def assert_offloaded(stack: Stack, sub: dict, *, task_clean: bool = True) -> None:
    """Task 与 Session 两边的事件历史里都只有引用，且用当前代码能回放（回放要能从 ObjectStore 取回对象）。"""
    task = h.fetch_one(stack.temporal_address, sub["taskId"])
    report = h.analyze(task)
    assert report.max_payload_bytes < MAX_INLINE_BYTES, f"超过阈值的载荷应由 Claim-Check 卸载，Task 历史里最大的有 {report.max_payload_bytes} 字节"
    if task_clean:
        h.assert_clean(report)
    sessions = h.fetch(stack.temporal_address, sub["sessionId"])
    for run in sessions:
        biggest = h.analyze(run).max_payload_bytes
        assert biggest < MAX_INLINE_BYTES, f"Session 历史里最大的载荷有 {biggest} 字节"
    h.assert_replays_python([task], env=stack.shared_store_env())
    h.assert_replays_go(stack.task_service_bin, sessions, env=stack.shared_store_env())


def test_large_task_input_is_offloaded_from_the_history(fstack: Stack, api: TaskApi) -> None:
    sub = api.submit("x" * (RESULT_KB * 1024))
    result = outcome(api, sub)
    assert result["status"] == "TASK_STATUS_DONE"
    # 回答超过 Node Executor 的内联上限，交付是外置结果的摘要与引用（S-05）。
    deliverable = result["deliverable"]["value"]
    assert deliverable["ref"].startswith("obj://t1/node-results/") and deliverable["size_bytes"] > RESULT_KB * 1024
    assert_offloaded(fstack, sub)


def test_claim_check_retries_while_the_object_store_is_temporarily_unavailable(fstack: Stack, api: TaskApi) -> None:
    # Worker 一侧的 Claim-Check 写 ObjectStore 时前两次报 transient：编解码器本地重试，工作流任务不失败。
    fstack.faults.set(Rule(id="objects-down", point="objects.put", action="transient", match=(cc_prefix(),), times=2))
    sub = api.submit("y" * (RESULT_KB * 1024))
    assert outcome(api, sub)["status"] == "TASK_STATUS_DONE"
    assert fstack.faults.fired("objects-down") == 2
    assert_offloaded(fstack, sub)


def test_claim_check_reports_a_persistent_outage_and_recovers(fstack: Stack, api: TaskApi) -> None:
    # 一直不可用：工作流任务失败，报错指明 ObjectStore 不可用；规则撤掉之后 Task 照常完成，没有丢东西。
    fstack.faults.set(Rule(id="objects-down", point="objects.put", action="transient", match=(cc_prefix(),)))
    sub = api.submit("z" * (RESULT_KB * 1024))
    wait_until(lambda: fstack.faults.fired("objects-down") >= 6, "Claim-Check 反复写 ObjectStore 失败", timeout=90)
    failures = h.analyze(h.fetch_one(fstack.temporal_address, sub["taskId"])).workflow_task_failures
    assert failures and all("claim_check_unavailable" in f and "ObjectStore" in f for f in failures), failures
    assert api.query(sub["taskId"])["status"] == "TASK_STATUS_RUNNING", "工作流还在，等 ObjectStore 恢复"

    fstack.faults.clear()
    assert outcome(api, sub)["status"] == "TASK_STATUS_DONE"
    assert_offloaded(fstack, sub, task_clean=False)


def cc_prefix() -> str:
    return "claim-check/"
