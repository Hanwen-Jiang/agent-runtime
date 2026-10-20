"""S-07 场景 4：模型供应商持续超时，触发 Model Gateway 切换。

设计书 3.3.1 第 3 条：失败带抖动重试，超过上限就切换同档位的其他供应商；附录 K.4：超过重试上限则交给 Reviewer。

Model Gateway 还没有实现（R-02），这里用故障 Worker 里的替身（e2e_support.fault_worker.FailoverStubGateway）：
同一档位两家供应商，每家先试 VENDOR_ATTEMPTS 次再切换。本场景检验的是 Runtime 一侧：
- 切换发生在一次 Node 调用之内，耗时超过心跳超时，Node Executor 在后台持续发心跳，activity 不被误判为卡死；
- 切换后结果的档位不变；
- 整个档位都不可用时，Node 按 retry 重试有限次，然后失败，交给 Reviewer，Task 不会卡住。
Model Gateway 实现之后，本场景改为对它跑，供应商一侧的故障由假的供应商端点注入。
"""

from __future__ import annotations

import pytest

from agentruntime_worker.workflows.timeouts import model_node_timeouts

from e2e_support import history as h
from e2e_support.api import TaskApi
from e2e_support.fault_worker import TIER, VENDOR_ATTEMPTS, VENDOR_TIMEOUT_S, VENDORS
from e2e_support.faults import Rule
from e2e_support.scenarios import answer_node, fault_stage, outcome, reviewer_saw, unique, vendor_calls
from e2e_support.stack import STUB_NODE_TIMEOUT_S, Stack

pytestmark = [pytest.mark.e2e, pytest.mark.faults, pytest.mark.scenario(4)]


def test_vendor_timeouts_switch_to_another_vendor_in_the_same_tier(fstack: Stack, api: TaskApi) -> None:
    marker = unique("m")
    first, second = VENDORS
    fstack.faults.set(Rule(id="vendor-a-down", point="model.vendor", action="timeout", match=(first, marker)))
    _, heartbeat = model_node_timeouts(STUB_NODE_TIMEOUT_S)
    assert VENDOR_ATTEMPTS * VENDOR_TIMEOUT_S > heartbeat.total_seconds(), "切换耗时要超过心跳超时，才能检验后台心跳"

    sub = api.submit(inputs=fault_stage(answer_node(marker=marker)))
    task_id = sub["taskId"]
    assert outcome(api, sub)["status"] == "TASK_STATUS_DONE"

    # 外部调用：第一家试满次数都超时，切到同档位的第二家，一次成功。
    assert len(vendor_calls(fstack, task_id, "start", first)) == VENDOR_ATTEMPTS
    assert len(vendor_calls(fstack, task_id, "timeout", first)) == VENDOR_ATTEMPTS
    assert len(vendor_calls(fstack, task_id, "start", second)) == 1
    ends = vendor_calls(fstack, task_id, "end")
    assert [c["vendor"] for c in ends] == [second] and ends[0]["tier"] == TIER

    task = h.fetch_one(fstack.temporal_address, task_id)
    report = h.analyze(task)
    h.assert_clean(report)
    node = report.activity("execute_model_node", "answer")
    assert node.attempts == 1, "切换在一次调用之内完成，activity 没有因为心跳超时而重试"
    assert node.result["meta"]["tier"] == TIER, "切换后档位不变"
    h.assert_replays_python([task])


def test_whole_tier_timing_out_exhausts_retries_and_reaches_the_reviewer(fstack: Stack, api: TaskApi) -> None:
    marker = unique("m")
    retry_max = 1
    fstack.faults.set(Rule(id="tier-down", point="model.vendor", action="timeout", match=(marker,)))

    sub = api.submit(inputs=fault_stage(answer_node(marker=marker, retry_max=retry_max)))
    task_id = sub["taskId"]
    assert outcome(api, sub)["status"] == "TASK_STATUS_ABORTED"

    attempts = retry_max + 1
    assert len(vendor_calls(fstack, task_id, "start")) == attempts * len(VENDORS) * VENDOR_ATTEMPTS, "重试有上限，不会无休止地调用供应商"
    assert vendor_calls(fstack, task_id, "end") == []
    saw = reviewer_saw(fstack, task_id)
    assert saw["statuses"]["answer"] == "failed"
    assert saw["errors"]["answer"]["failure_class"] == "transient"

    task = h.fetch_one(fstack.temporal_address, task_id)
    report = h.analyze(task)
    h.assert_clean(report, allowed_failures=[("execute_model_node", "answer")])
    node = report.activity("execute_model_node", "answer")
    assert node.outcome == "failed" and node.attempts == attempts
    h.assert_replays_python([task])
