"""S-05 验收：Node Executor 的 activity（设计书 3.2.4；附录 D、K.4；计划 S-05 难点）。

在 ActivityEnvironment 里直接调用真实的 NodeExecutor，依赖是 S-02 的开发实现：
幂等键、审批双保险、工具 effect、失败分类与重试映射、output_schema、model 重问、rationale 只进 trace、
缓存、大结果外置、短期凭据、结果未知时的对账、human 问题的发出与取消、sandbox 执行、心跳。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from agentruntime_core.dev.secret_store import LocalFileSecretStore
from agentruntime_core.enums import Effect
from agentruntime_core.errors import DeniedError, InvalidError, TransientError
from agentruntime_core.providers.human_channel import QuestionStatus
from agentruntime_core.providers.secret_store import LeaseStatus
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker import names
from agentruntime_worker.activities.types import GateRecord, HumanCancel, HumanQuestion
from agentruntime_worker.executor import keys
from agentruntime_worker.executor import node_executor as ne
from agentruntime_worker.executor.node_executor import ExecutorConfig

from .executor_harness import (
    OTHER_TENANT,
    RATE_SCHEMA,
    TASK_ID,
    TENANT,
    ExecutorFixture,
    ExternalLedger,
    ResponseLost,
    approved,
    make_fixture,
    model,
    node_input,
    program,
    register_external,
)
from .support import assert_valid


@pytest.fixture
def fx(tmp_path: Path) -> ExecutorFixture:
    return make_fixture(tmp_path)


def env_for(name: str, *, queue: str = names.PROGRAM_QUEUE, attempt: int = 1, heartbeat: float | None = 2) -> ActivityEnvironment:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info,
        activity_type=name,
        task_queue=queue,
        attempt=attempt,
        workflow_id=TASK_ID,
        heartbeat_timeout=timedelta(seconds=heartbeat) if heartbeat else None,
        start_to_close_timeout=timedelta(seconds=30),
    )
    return env


async def run_program(fx: ExecutorFixture, node: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return await env_for(names.EXECUTE_PROGRAM_NODE).run(fx.executor.execute_program_node, node_input(node, **kw))


async def run_model(fx: ExecutorFixture, node: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return await env_for(names.EXECUTE_MODEL_NODE, queue=names.MODEL_QUEUE).run(fx.executor.execute_model_node, node_input(node, **kw))


async def expect_error(coro: Any, error_type: str, *, retryable: bool = False) -> ApplicationError:
    with pytest.raises(ApplicationError) as err:
        await coro
    assert err.value.type == error_type, f"应为 {error_type}，实际 {err.value.type}：{err.value}"
    assert err.value.non_retryable is (not retryable), f"{error_type} 的 non_retryable 应为 {not retryable}"
    return err.value


def tenant_proto(t: Any = TENANT) -> TenantContext:
    return TenantContext(tenant_id=t.tenant_id, user_id=t.user_id, session_id=t.session_id)


# --- program 节点 ---


async def test_program_node_invokes_the_tool_named_by_its_template(fx: ExecutorFixture) -> None:
    out = await run_program(fx, program("q", "lookup_rate", inputs={"region": "east"}))
    assert out == {"result": {"echo": {"region": "east"}}, "meta": out["meta"]}
    assert_valid("node_output", out)
    assert "rationale" not in out
    assert fx.calls["lookup_rate"] == 1
    invocation = fx.invocations[-1]
    assert invocation.tenant_id == "t1", "工具拿到的租户来自作用域，不来自参数"
    assert invocation.idempotency_key is None, "只读节点不带幂等键"
    assert invocation.task_id == TASK_ID and invocation.session_id == TENANT.session_id, "工具知道是哪个 Task、哪个会话（D-13、D-16）"


async def test_sandbox_cost_reported_by_a_tool_goes_into_the_node_cost(fx: ExecutorFixture) -> None:
    """D-11：平台内工具在沙箱里跑完时报出 sandbox_cost_usd，Node 的 meta.cost_usd 带上它，Stage Runner 把它算进 Round 与 Task。"""

    async def costly(invocation: Any) -> dict[str, Any]:
        return {"exit_code": 0, "sandbox_cost_usd": 0.0125}

    fx.behaviors["lookup_rate"] = costly
    out = await run_program(fx, program("q", "lookup_rate"))
    assert out["meta"]["cost_usd"] == pytest.approx(0.0125)
    fx.behaviors["lookup_rate"] = lambda inv: _done({"sandbox_cost_usd": "not a number"})
    assert "cost_usd" not in (await run_program(fx, program("q2", "lookup_rate")))["meta"]


async def _done(value: Any) -> Any:
    return value


async def test_tool_with_a_higher_effect_than_the_node_is_denied(fx: ExecutorFixture) -> None:
    node = program("w", "update_crm", effect="read")
    await expect_error(run_program(fx, node), ne.DENIED)
    assert fx.calls["update_crm"] == 0, "Node 声明只读，工具却会写：拒绝，不执行"


async def test_unknown_tool_is_a_non_retryable_error(fx: ExecutorFixture) -> None:
    await expect_error(run_program(fx, program("q", "no_such_tool")), ne.INVALID)


# --- 幂等键 ---


async def test_write_nodes_require_the_code_generated_idempotency_key(fx: ExecutorFixture) -> None:
    missing = program("w", "update_crm", effect="write_reversible", with_key=False)
    await expect_error(run_program(fx, missing), ne.DENIED)
    forged = program("w", "update_crm", effect="write_reversible", with_key=False, idempotency_key="idk_written_by_the_model")
    await expect_error(run_program(fx, forged), ne.DENIED)
    assert fx.calls["update_crm"] == 0

    good = program("w", "update_crm", effect="write_reversible", inputs={"customer": "C-1"})
    await run_program(fx, good)
    assert fx.invocations[-1].idempotency_key == keys.idempotency_key("t1", TASK_ID, "w", {"customer": "C-1"})


async def test_same_write_retried_takes_effect_once(fx: ExecutorFixture) -> None:
    node = program("w", "update_crm", effect="write_reversible", inputs={"customer": "C-1"})
    first = await run_program(fx, node)
    again = await env_for(names.EXECUTE_PROGRAM_NODE, attempt=2).run(fx.executor.execute_program_node, node_input(node))
    assert fx.calls["update_crm"] == 1, "activity 重试用同一幂等键，外部只生效一次"
    assert again["result"] == first["result"]


# --- 审批双保险 ---


@pytest.mark.parametrize(
    "gate",
    [
        None,
        GateRecord(gate_node="pay.1", option="reject", approved_by="u7"),
        GateRecord(gate_node="other.1", option="approve", approved_by="u7"),
    ],
)
async def test_write_irreversible_without_an_approved_gate_is_rejected(fx: ExecutorFixture, gate: GateRecord | None) -> None:
    node = program("pay", "pay", effect="write_irreversible")
    await expect_error(run_program(fx, node, gate=gate), ne.DENIED)
    assert fx.calls["pay"] == 0


async def test_write_irreversible_with_an_approved_gate_runs(fx: ExecutorFixture) -> None:
    await run_program(fx, program("pay", "pay", effect="write_irreversible"), gate=approved("pay"))
    assert fx.calls["pay"] == 1


# --- 失败分类与重试映射 ---


def raising(error: Exception):
    async def behave(_: Any) -> Any:
        raise error

    return behave


@pytest.mark.parametrize(
    ("error", "error_type", "retryable"),
    [
        (TransientError("rate_limit", "限流"), ne.TRANSIENT, True),
        (TransientError("timeout", "超时"), ne.TRANSIENT, True),
        (InvalidError("bad_request", "参数不对"), ne.INVALID, False),
        (DeniedError("forbidden", "上游拒绝"), ne.DENIED, False),
        (RuntimeError("tool bug"), ne.INTERNAL, False),
    ],
)
async def test_failure_classification(fx: ExecutorFixture, error: Exception, error_type: str, retryable: bool) -> None:
    fx.behaviors["lookup_rate"] = raising(error)
    await expect_error(run_program(fx, program("q", "lookup_rate")), error_type, retryable=retryable)


async def test_retry_on_limits_which_transient_errors_are_retried(fx: ExecutorFixture) -> None:
    fx.behaviors["lookup_rate"] = raising(TransientError("timeout", "超时"))
    node = program("q", "lookup_rate", retry={"max": 2, "on": ["rate_limit"]})
    await expect_error(run_program(fx, node), ne.TRANSIENT, retryable=False)


async def test_invalid_arguments_never_reach_the_tool(fx: ExecutorFixture) -> None:
    # 工具的 input_schema 由 ToolDriver 校验；参数错误不可重试。
    await expect_error(run_program(fx, program("q", "strict_input", inputs={"region": 7})), ne.INVALID)
    assert fx.calls["strict_input"] == 0


async def test_tool_output_violating_its_schema_is_invalid_output(fx: ExecutorFixture) -> None:
    async def wrong(_: Any) -> Any:
        return {"rate": "高"}

    fx.behaviors["strict_output"] = wrong
    await expect_error(run_program(fx, program("q", "strict_output")), ne.INVALID_OUTPUT)


async def test_permission_not_granted_is_denied(fx: ExecutorFixture) -> None:
    await expect_error(run_program(fx, program("q", "crm_read")), ne.DENIED)
    assert fx.calls["crm_read"] == 0


# --- output_schema ---


async def test_program_result_is_checked_against_the_node_output_schema(fx: ExecutorFixture) -> None:
    await expect_error(run_program(fx, program("q", "lookup_rate", output_schema=RATE_SCHEMA)), ne.INVALID_OUTPUT)

    async def rate(_: Any) -> Any:
        return {"rate": 0.12}

    fx.behaviors["lookup_rate"] = rate
    out = await run_program(fx, program("q2", "lookup_rate", output_schema="region_rate_v1"))
    assert out["result"] == {"rate": 0.12}, "登记名的 schema 由 resolver 提供"
    await expect_error(run_program(fx, program("q3", "lookup_rate", output_schema="unregistered_v1")), ne.INVALID)


# --- model 节点 ---


async def test_model_node_calls_the_gateway_with_tenant_and_call_context(fx: ExecutorFixture) -> None:
    fx.models.reply({"summary": "物流延迟"})
    node = model("analyze", tier="strong@medium", reasoning="plan_first")
    out = await run_model(fx, node, injections=[{"zone": "brief", "content": "只用公开数据"}])
    assert out["result"] == {"summary": "物流延迟"}
    assert_valid("node_output", out)
    request = fx.models.requests[0]
    assert request.tenant.tenant_id == "t1" and request.tenant.session_id == TENANT.session_id
    assert request.call.task_id == TASK_ID and request.call.node_id == "analyze" and request.call.tier == "strong@medium"
    text = "\n".join(m.text for m in request.generation.messages)
    assert "只用公开数据" in text and "华东退款率上升" in text, "inject 的内容与 Node 输入都进了上下文"
    assert out["meta"]["tier"] == "strong@medium"


async def test_model_rationale_goes_to_the_trace_only(fx: ExecutorFixture) -> None:
    fx.models.reply({"summary": "x"}, rationale="SECRET-REASONING")
    out = await run_model(fx, model("analyze"))
    assert "SECRET-REASONING" not in json.dumps(out, ensure_ascii=False), "rationale 不进 activity 的结果"
    await fx.telemetry.bind(tenant_proto()).flush()
    node_spans = [s for s in fx.telemetry.exported_spans if s.attributes.get("node_id") == "analyze"]
    assert node_spans, "每次执行一个 span，带 node_id"
    assert any("SECRET-REASONING" in event for s in node_spans for event in s.events), "rationale 作为 span 事件写入 trace"


async def test_model_with_invalid_output_is_asked_once_more_with_the_errors(fx: ExecutorFixture) -> None:
    fx.models.reply({"rate": "高"}, {"rate": 0.3})
    out = await run_model(fx, model("analyze", output_schema=RATE_SCHEMA))
    assert out["result"] == {"rate": 0.3}
    assert len(fx.models.requests) == 2
    retry_text = "\n".join(m.text for m in fx.models.requests[1].generation.messages)
    assert "rate" in retry_text and "number" in retry_text, "重问时带上校验错误"


async def test_model_invalid_twice_is_invalid_output(fx: ExecutorFixture) -> None:
    fx.models.reply({"rate": "高"}, {"rate": "很高"})
    await expect_error(run_model(fx, model("analyze", output_schema=RATE_SCHEMA)), ne.INVALID_OUTPUT)
    assert len(fx.models.requests) == 2, "只重问一次"


# --- 缓存 ---


async def test_read_node_with_cache_is_served_from_the_cache(fx: ExecutorFixture) -> None:
    node = program("q", "lookup_rate", inputs={"region": "east"}, cache={"ttl_s": 60})
    first = await run_program(fx, node)
    second = await run_program(fx, {**node, "id": "q_again"})
    assert fx.calls["lookup_rate"] == 1, "同模板版本、同输入命中缓存"
    assert second["result"] == first["result"]
    await run_program(fx, {**node, "template": "lookup_rate@v1", "inputs": {"region": "west"}})
    assert fx.calls["lookup_rate"] == 2, "输入不同不命中"
    await run_program(fx, node, tenant=OTHER_TENANT)
    assert fx.calls["lookup_rate"] == 3, "缓存按租户隔离"


async def test_nodes_without_cache_or_with_writes_are_not_cached(fx: ExecutorFixture) -> None:
    plain = program("q", "lookup_rate")
    await run_program(fx, plain)
    await run_program(fx, plain)
    assert fx.calls["lookup_rate"] == 2, "没声明 cache 的不缓存"
    write = program("w", "update_crm", effect="write_reversible", cache={"ttl_s": 60})
    await run_program(fx, write)
    other_task = "tsk_01J" + "0" * 22 + "F"
    await run_program(fx, program("w", "update_crm", effect="write_reversible", cache={"ttl_s": 60}, task_id=other_task), task_id=other_task)
    assert fx.calls["update_crm"] == 2, "写操作不缓存"


# --- 大结果 ---


async def test_large_results_are_moved_to_the_object_store(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path, config=ExecutorConfig(inline_result_limit=200, preview_chars=50))

    async def big(_: Any) -> Any:
        return {"rows": [{"region": f"r{i}", "rate": i / 100} for i in range(50)]}

    fx.behaviors["lookup_rate"] = big
    out = await run_program(fx, program("q", "lookup_rate"))
    ref = out["meta"]["result_ref"]
    assert ref["uri"].startswith("obj://t1/"), "路径带租户前缀"
    assert out["result"]["ref"] == ref["uri"] and len(out["result"]["summary"]) <= 50
    assert_valid("node_output", out)
    store = fx.objects.bind(tenant_proto())
    data = await store.get(store.key_of(ref["uri"]))
    assert json.loads(data.data) == await big(None)
    assert ref["size_bytes"] == len(data.data) and ref["sha256"] == data.info.sha256
    again = await run_program(fx, program("q", "lookup_rate"))
    assert again["meta"]["result_ref"]["uri"] == ref["uri"], "重试写同一个对象"


# --- 凭据 ---


async def test_tools_get_a_short_lived_credential_that_is_revoked_after_the_call(fx: ExecutorFixture) -> None:
    fx.tools.grant("t1", "crm.read")
    fx.secrets.grant_role("t1", "tool.crm_read")
    out = await run_program(fx, program("q", "crm_read"))
    credential = fx.invocations[-1].credential
    assert credential is not None and credential.role == "tool.crm_read"
    assert credential.expires_at - datetime.now(UTC) <= timedelta(minutes=15)
    assert await fx.secrets.bind(tenant_proto()).lease_status(credential.lease_id) == LeaseStatus.REVOKED
    plaintext = credential.secret.reveal()
    assert plaintext not in fx.telemetry_output.getvalue() and plaintext not in json.dumps(out, ensure_ascii=False)


async def test_credential_is_revoked_even_when_the_tool_fails(fx: ExecutorFixture) -> None:
    fx.tools.grant("t1", "crm.read")
    fx.secrets.grant_role("t1", "tool.crm_read")
    seen: list[Any] = []

    async def fail(invocation: Any) -> Any:
        seen.append(invocation.credential)
        raise RuntimeError("boom")

    fx.behaviors["crm_read"] = fail
    await expect_error(run_program(fx, program("q", "crm_read")), ne.INTERNAL)
    assert await fx.secrets.bind(tenant_proto()).lease_status(seen[0].lease_id) == LeaseStatus.REVOKED


async def test_credential_role_not_granted_is_denied(fx: ExecutorFixture) -> None:
    fx.tools.grant("t1", "crm.read")
    await expect_error(run_program(fx, program("q", "crm_read")), ne.DENIED)
    assert fx.calls["crm_read"] == 0


# R-04 评审本期修第 2、3 条（docs/plan/review-R-04-secretstore.md 第 4 节）：工具回显的凭据在出口处换成掩码；吊销失败要看得见。


def grant_crm(fx: ExecutorFixture) -> None:
    fx.tools.grant("t1", "crm.read")
    fx.secrets.grant_role("t1", "tool.crm_read")


async def node_events(fx: ExecutorFixture, node_id: str) -> list[str]:
    await fx.telemetry.bind(tenant_proto()).flush()
    return [e for s in fx.telemetry.exported_spans if s.attributes.get("node_id") == node_id for e in s.events]


async def test_credential_echoed_in_the_tool_output_is_masked(fx: ExecutorFixture) -> None:
    grant_crm(fx)

    async def echo(invocation: Any) -> Any:
        token = invocation.credential.secret.reveal()
        return {"auth": token, "header": f"Bearer {token}", "rows": [{"note": f"conn=crm://svc:{token}@db"}]}

    fx.behaviors["crm_read"] = echo
    out = await run_program(fx, program("q", "crm_read"))
    plaintext = fx.invocations[-1].credential.secret.reveal()
    assert plaintext not in json.dumps(out, ensure_ascii=False), "凭据原文不进 Node 结果（事件历史、下游、Reviewer）"
    assert out["result"]["auth"] == "***" and out["result"]["header"] == "Bearer ***", "原文换成掩码，其余内容不变"
    assert out["result"]["rows"] == [{"note": "conn=crm://svc:***@db"}]
    events = await node_events(fx, "q")
    assert any(e.startswith("credential_redacted") and "crm_read" in e for e in events), "出口处换过掩码要在 trace 里看得到"
    assert plaintext not in fx.telemetry_output.getvalue()


async def test_credential_echoed_in_a_tool_error_is_masked(fx: ExecutorFixture) -> None:
    grant_crm(fx)

    async def fail(invocation: Any) -> Any:
        raise InvalidError("bad_request", f"上游拒绝：Authorization: Bearer {invocation.credential.secret.reveal()}")

    fx.behaviors["crm_read"] = fail
    err = await expect_error(run_program(fx, program("q", "crm_read")), ne.INVALID)
    plaintext = fx.invocations[-1].credential.secret.reveal()
    assert plaintext not in str(err) and "Bearer ***" in str(err), "错误信息进事件历史与 Reviewer 的输入，凭据原文换成掩码"
    assert plaintext not in fx.telemetry_output.getvalue()


async def test_failed_revocation_is_recorded_and_the_node_still_succeeds(
    fx: ExecutorFixture, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    grant_crm(fx)

    async def unavailable(self: Any, lease_id: str) -> None:
        raise TransientError("unavailable", "SecretStore 暂时不可用")

    monkeypatch.setattr(LocalFileSecretStore, "revoke", unavailable)
    with caplog.at_level(logging.WARNING):
        out = await run_program(fx, program("q", "crm_read"))
    credential = fx.invocations[-1].credential
    assert out["result"] == {"echo": dict(fx.invocations[-1].arguments)}, "吊销失败不改变 Node 的结局"
    events = [e for e in await node_events(fx, "q") if e.startswith("credential_revoke_failed")]
    assert events and credential.lease_id in events[0] and "crm_read" in events[0] and "unavailable" in events[0], "trace 里记下工具、租约与错误 code"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(credential.lease_id in m and "crm_read" in m for m in warnings), "另记一条警告日志"
    plaintext = credential.secret.reveal()
    assert plaintext not in caplog.text and plaintext not in fx.telemetry_output.getvalue()


# --- 结果未知：按幂等键对账 ---


async def test_lost_write_response_is_reconciled_by_idempotency_key(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path, tools=lambda inner: ResponseLost(inner, lose=lambda name: name in ("update_crm", "pay")))
    out = await run_program(fx, program("w", "update_crm", effect="write_reversible", inputs={"c": 1}))
    assert out["result"] == {"echo": {"c": 1}} and fx.calls["update_crm"] == 1, "外部已生效：取当时的结果，不再执行"
    out = await run_program(fx, program("pay", "pay", effect="write_irreversible", inputs={"amount": 9}), gate=approved("pay"))
    assert out["result"] == {"echo": {"amount": 9}} and fx.calls["pay"] == 1


async def test_write_timeout_without_effect(fx: ExecutorFixture) -> None:
    fx.behaviors["update_crm"] = raising(TransientError("timeout", "超时"))
    fx.behaviors["pay"] = raising(TransientError("timeout", "超时"))
    # 对账查不到：可逆写操作可以带同一幂等键重试；不可逆写操作记为结果未知，交给 Reviewer。
    await expect_error(run_program(fx, program("w", "update_crm", effect="write_reversible")), ne.TRANSIENT, retryable=True)
    await expect_error(run_program(fx, program("pay", "pay", effect="write_irreversible"), gate=approved("pay")), ne.UNKNOWN)
    assert fx.calls["pay"] == 1


async def test_write_irreversible_without_lookup_support_is_unknown(fx: ExecutorFixture) -> None:
    fx.behaviors["send_mail"] = raising(TransientError("timeout", "超时"))
    err = await expect_error(run_program(fx, program("m", "send_mail", effect="write_irreversible"), gate=approved("m")), ne.UNKNOWN)
    assert "查询" in str(err)


async def test_retried_write_looks_up_the_external_state_before_executing(fx: ExecutorFixture) -> None:
    ledger = ExternalLedger()
    register_external(fx, "ledger_append", Effect.WRITE_REVERSIBLE, ledger)
    node = program("w", "ledger_append", effect="write_reversible", inputs={"c": 1})
    # 上一次 attempt 在外部生效后 Worker 崩溃：进程内没有记录，外部有。
    ledger.records[node["idempotency_key"]] = {"txn": "txn-crashed"}
    env = env_for(names.EXECUTE_PROGRAM_NODE, attempt=2)
    out = await env.run(fx.executor.execute_program_node, node_input(node))
    assert out["result"] == {"txn": "txn-crashed"} and ledger.applied == [], "重试前先查，外部已生效就不再执行"

    fresh = program("w2", "ledger_append", effect="write_reversible", inputs={"c": 2})
    out = await env.run(fx.executor.execute_program_node, node_input(fresh))
    assert ledger.applied == [fresh["idempotency_key"]], "确认没有生效才执行，执行一次"


async def test_lookup_unavailable_while_reconciling_a_lost_write(fx: ExecutorFixture) -> None:
    ledger = ExternalLedger(lose_response=True)
    register_external(fx, "ledger_transfer", Effect.WRITE_IRREVERSIBLE, ledger)
    register_external(fx, "ledger_append", Effect.WRITE_REVERSIBLE, ledger)
    ledger.lookup_failures = 2
    # 不可逆写操作：结果未知、这次查不了，报 reconcile，交给工作流对账；不重试。
    err = await expect_error(run_program(fx, program("pay", "ledger_transfer", effect="write_irreversible"), gate=approved("pay")), ne.RECONCILE)
    assert "对账" in str(err)
    # 可逆写操作：报 transient，下一次 attempt 先查再决定。
    await expect_error(run_program(fx, program("w", "ledger_append", effect="write_reversible")), ne.TRANSIENT, retryable=True)
    assert len(ledger.applied) == 2


async def test_reconcile_node_activity(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path)
    node = program("w", "update_crm", effect="write_reversible", inputs={"c": 2})
    env = env_for(names.RECONCILE_NODE)
    missing = await env.run(fx.executor.reconcile_node, node_input(node))
    assert missing.found is False
    await run_program(fx, node)
    found = await env.run(fx.executor.reconcile_node, node_input(node))
    assert found.found is True and found.output["result"] == {"echo": {"c": 2}}
    assert fx.calls["update_crm"] == 1, "对账只查询，不执行"
    unsupported = await env.run(fx.executor.reconcile_node, node_input(program("m", "send_mail", effect="write_irreversible")))
    assert unsupported.found is False and unsupported.reason


# --- human 节点 ---


async def test_ask_and_cancel_go_through_the_human_channel(fx: ExecutorFixture) -> None:
    deadline = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    q = HumanQuestion(tenant=TENANT, task_id=TASK_ID, question_id="confirm", text="按这个范围继续吗？", options=["approve", "reject"], deadline=deadline)
    await env_for(names.ASK_HUMAN, queue=names.DECISION_QUEUE).run(fx.executor.ask_human, q)
    await env_for(names.ASK_HUMAN, queue=names.DECISION_QUEUE).run(fx.executor.ask_human, q)  # 重试同样的问题不报错
    channel = fx.humans.bind(tenant_proto())
    channel_id = ne.channel_question_id(TASK_ID, 1, "confirm")
    assert channel_id == f"{TASK_ID}/r1/confirm" and ne.workflow_question_id(channel_id) == "confirm", "通道里的问题 id 带 Task 与 Round"
    assert await channel.status(channel_id) == QuestionStatus.PENDING
    assert "按这个范围继续吗" in fx.human_output.getvalue()
    await env_for(names.CANCEL_HUMAN, queue=names.DECISION_QUEUE).run(fx.executor.cancel_human, HumanCancel(tenant=TENANT, task_id=TASK_ID, question_id="confirm"))
    assert await channel.status(channel_id) == QuestionStatus.CANCELLED


async def test_withdrawing_a_question_the_channel_no_longer_knows_succeeds(fx: ExecutorFixture) -> None:
    """S-07 场景 5：问题已不在通道里（例如开发用的通道随 Worker 重启丢了），撤回按已撤回处理，不报错。"""
    gone = HumanCancel(tenant=TENANT, task_id=TASK_ID, question_id="never_asked")
    await env_for(names.CANCEL_HUMAN, queue=names.DECISION_QUEUE).run(fx.executor.cancel_human, gone)


# --- sandbox 与心跳 ---


async def test_sandbox_template_runs_code_and_destroys_the_environment(fx: ExecutorFixture) -> None:
    node = program("calc", names.SANDBOX_TEMPLATE, effect="none", inputs={"runtime": "python", "code": "print(6 * 7)"})
    out = await run_program(fx, node)
    assert out["result"]["exit_code"] == 0 and out["result"]["stdout"].strip() == "42"
    assert out["result"]["timed_out"] is False


async def test_long_tool_calls_keep_heartbeating(fx: ExecutorFixture) -> None:
    async def slow(_: Any) -> Any:
        await asyncio.sleep(1.2)
        return {"ok": True}

    fx.behaviors["lookup_rate"] = slow
    env = env_for(names.EXECUTE_PROGRAM_NODE, heartbeat=0.6)
    beats: list[Any] = []
    env.on_heartbeat = lambda *d: beats.append(d)
    await env.run(fx.executor.execute_program_node, node_input(program("q", "lookup_rate")))
    assert len(beats) >= 2, "执行期间持续发心跳，间隔短于 heartbeat 超时"


async def test_cancellation_reaches_the_running_tool(fx: ExecutorFixture) -> None:
    started = asyncio.Event()

    async def hang(_: Any) -> Any:
        started.set()
        await asyncio.sleep(3600)

    fx.behaviors["lookup_rate"] = hang
    env = env_for(names.EXECUTE_PROGRAM_NODE)
    run = asyncio.create_task(env.run(fx.executor.execute_program_node, node_input(program("q", "lookup_rate"))))
    await asyncio.wait_for(started.wait(), 5)
    env.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 5)
