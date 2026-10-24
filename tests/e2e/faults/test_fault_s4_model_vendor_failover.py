"""S-07 场景 4：模型供应商持续超时，触发 Model Gateway 切换。

设计书 3.3.1 第 3 条：失败带抖动重试，超过上限就切换同档位的其他供应商；附录 K.4：超过重试上限则交给 Reviewer。

前两个用例用故障 Worker 里的替身（e2e_support.fault_worker.FailoverStubGateway）：同一档位两家供应商，每家先试
VENDOR_ATTEMPTS 次再切换。它们检验的是 Runtime 一侧：
- 切换发生在一次 Node 调用之内，耗时超过心跳超时，Node Executor 在后台持续发心跳，activity 不被误判为卡死；
- 切换后结果的档位不变；
- 整个档位都不可用时，Node 按 retry 重试有限次，然后失败，交给 Reviewer，Task 不会卡住。
后两个用例对真的 Go Model Gateway 跑（T-13）：故障由假的上游端点按模型注入，检验网关的同档切换与 Runtime 的接法。
"""

from __future__ import annotations

import pytest

from agentruntime_worker.workflows.timeouts import model_node_timeouts

from e2e_support import fake_openai
from e2e_support import history as h
from e2e_support.api import TaskApi
from e2e_support.fake_openai import SERVER_ERROR, FakeOpenAI
from e2e_support.fault_worker import TIER, VENDOR_ATTEMPTS, VENDOR_TIMEOUT_S, VENDORS
from e2e_support.fault_gateway import GATEWAY_ATTEMPTS_PER_PROFILE, MID_B, MID_TIER, MODEL_A, MODEL_B
from e2e_support.faults import Rule
from e2e_support.gateway import Gateway
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


# --- 经真 Model Gateway（T-13，docs/plan/live-e2e.md 第 3 节第 5 条）：故障由假的上游端点按模型注入 ---


def test_gateway_switches_to_the_next_profile_when_one_upstream_model_keeps_failing(
    gateway_fstack: Stack, api: TaskApi, fault_gateway: Gateway, fault_upstream: FakeOpenAI
) -> None:
    fault_upstream.fail_model(MODEL_A, SERVER_ERROR)
    mark = fault_upstream.mark()
    sub = api.submit(inputs=fault_stage(answer_node(marker=unique("m"))))
    task_id = sub["taskId"]
    result = outcome(api, sub)
    assert result["status"] == "TASK_STATUS_DONE"
    assert result["deliverable"]["value"]["text"] == fake_openai.ANSWER, "交付文本来自上游，不是替身"

    # 外部调用：A 试满网关的次数都失败，切到同档的 B，一次成功。
    models = [r.model for r in fault_upstream.since(mark, "/chat/completions")]
    assert models.count(MODEL_A) == GATEWAY_ATTEMPTS_PER_PROFILE
    assert models.count(MODEL_B) == 1 and models[-1] == MODEL_B

    ok = [r for r in fault_gateway.metering_for(tenant_id="t1", task_id=task_id, node_id="answer") if r.get("status", "ok") == "ok"]
    assert [r["model_profile_id"] for r in ok] == [MID_B], "计量记的是实际服务的 Profile"
    assert ok[0]["tier"] == MID_TIER

    task = h.fetch_one(gateway_fstack.temporal_address, task_id)
    report = h.analyze(task)
    h.assert_clean(report)
    node = report.activity("execute_model_node", "answer")
    assert node.attempts == 1, "切换在网关的一次调用之内完成，activity 不重试"
    assert node.result["meta"]["tier"] == MID_TIER, "切换后档位不变"
    h.assert_replays_python([task])


def test_gateway_with_the_whole_tier_failing_exhausts_retries_and_reaches_the_reviewer(
    gateway_fstack: Stack, api: TaskApi, fault_gateway: Gateway, fault_upstream: FakeOpenAI
) -> None:
    retry_max = 1
    fault_upstream.fail_model(MODEL_A, SERVER_ERROR)
    fault_upstream.fail_model(MODEL_B, SERVER_ERROR)
    mark = fault_upstream.mark()
    sub = api.submit(inputs=fault_stage(answer_node(marker=unique("m"), retry_max=retry_max)))
    task_id = sub["taskId"]
    assert outcome(api, sub)["status"] == "TASK_STATUS_ABORTED"

    attempts = retry_max + 1
    calls = fault_upstream.since(mark, "/chat/completions")
    assert {r.model for r in calls} == {MODEL_A, MODEL_B}, "每次 activity 都把同档的两个 Profile 试过"
    assert len(calls) <= attempts * 2 * GATEWAY_ATTEMPTS_PER_PROFILE, "重试有上限，不会无休止地调用上游"
    assert not [r for r in fault_gateway.metering_for(tenant_id="t1", task_id=task_id, node_id="answer") if r.get("status", "ok") == "ok"]
    saw = reviewer_saw(gateway_fstack, task_id)
    assert saw["statuses"]["answer"] == "failed"
    assert saw["errors"]["answer"]["failure_class"] == "transient"

    task = h.fetch_one(gateway_fstack.temporal_address, task_id)
    report = h.analyze(task)
    h.assert_clean(report, allowed_failures=[("execute_model_node", "answer")])
    node = report.activity("execute_model_node", "answer")
    assert node.outcome == "failed" and node.attempts == attempts
    h.assert_replays_python([task])
