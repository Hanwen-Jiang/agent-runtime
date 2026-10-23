"""T-10 验收：tool_loop 的 call_tool 真的经 ToolDriver 调用工具（细化见 docs/plan/tool-driver.md 第 2 节第 2 条、第 3 节）。

在 ActivityEnvironment 里直接调用真实的 NodeExecutor.call_tool，依赖是 S-02 的开发实现（executor_harness）。覆盖：
- 按名字取当前登记的最大版本；没登记的工具拒绝。
- 工具登记的 effect 高于工作流批准的 effect：拒绝，不调用。
- 写操作的幂等键由代码按 tenant_id、task_id、这次调用的 id 与参数生成；不可逆工具没有这次调用自己的审批记录就拒绝。
- 要权限的工具拿 SecretStore 签发的短期凭据，用完吊销；大结果写 ObjectStore，只回摘要与引用；读工具的临时错误可重试。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

from agentruntime_core.enums import Effect
from agentruntime_core.errors import TransientError
from agentruntime_core.providers.secret_store import LeaseStatus
from agentruntime_core.providers.tool_driver import ToolInvocation, ToolSpec

from agentruntime_worker import names
from agentruntime_worker.activities.types import GateRecord, ToolCallInput
from agentruntime_worker.executor import keys

from .executor_harness import TASK_ID, TENANT, ExecutorFixture, make_fixture
from .test_node_executor import env_for, tenant_proto

CALL_ID = "fix.4"


@pytest.fixture
def fx(tmp_path: Path) -> ExecutorFixture:
    return make_fixture(tmp_path)


def call(tool: str, effect: str, args: dict[str, Any] | None = None, gate: GateRecord | None = None) -> ToolCallInput:
    return ToolCallInput(tenant=TENANT, task_id=TASK_ID, round_seq=1, node_id=CALL_ID, tool=tool, args=args or {}, effect=effect, gate=gate)


async def run(fx: ExecutorFixture, inp: ToolCallInput) -> Any:
    return await env_for(names.CALL_TOOL).run(fx.executor.call_tool, inp)


async def test_calls_the_registered_tool(fx: ExecutorFixture) -> None:
    out = await run(fx, call("lookup_rate", "read", {"region": "华东"}))
    assert out == {"echo": {"region": "华东"}}
    assert fx.calls["lookup_rate"] == 1


async def test_uses_the_highest_registered_version(fx: ExecutorFixture) -> None:
    async def v10(invocation: ToolInvocation) -> Any:
        return {"version": 10}

    spec = ToolSpec(name="lookup_rate", version="v10", description="新版", input_schema={"type": "object"}, output_schema={"type": "object"},
                    effect=Effect.READ, permissions=frozenset(), timeout=timedelta(seconds=5))
    fx.tools.register(spec, v10)
    assert await run(fx, call("lookup_rate", "read")) == {"version": 10}


async def test_an_unregistered_tool_is_denied(fx: ExecutorFixture) -> None:
    with pytest.raises(ApplicationError) as caught:
        await run(fx, call("ghost_tool", "write_irreversible"))
    assert caught.value.non_retryable and caught.value.type == "denied"


async def test_a_tool_above_the_approved_effect_is_denied_and_not_called(fx: ExecutorFixture) -> None:
    with pytest.raises(ApplicationError) as caught:
        await run(fx, call("update_crm", "read", {"id": 7}))
    assert caught.value.non_retryable and caught.value.type == "denied"
    assert fx.calls["update_crm"] == 0, "两次查询之间换了版本也不会把写操作当成只读执行"


async def test_writes_carry_a_key_derived_from_the_call(fx: ExecutorFixture) -> None:
    args = {"id": 7, "status": "closed"}
    await run(fx, call("update_crm", "write_reversible", args))
    assert fx.invocations[-1].idempotency_key == keys.idempotency_key(TENANT.tenant_id, TASK_ID, CALL_ID, args)


async def test_an_irreversible_call_needs_its_own_approval(fx: ExecutorFixture) -> None:
    with pytest.raises(ApplicationError) as caught:
        await run(fx, call("pay", "write_irreversible", {"amount": 30}))
    assert caught.value.type == "denied" and fx.calls["pay"] == 0
    with pytest.raises(ApplicationError):
        await run(fx, call("pay", "write_irreversible", {"amount": 30}, gate=GateRecord(gate_node="other.2.1", option="approve", approved_by="u1")))
    assert fx.calls["pay"] == 0, "审批记录必须是这次调用自己的（<调用 id>.1）"
    await run(fx, call("pay", "write_irreversible", {"amount": 30}, gate=GateRecord(gate_node=f"{CALL_ID}.1", option="approve", approved_by="u1")))
    assert fx.calls["pay"] == 1


async def test_tools_with_permissions_get_a_short_lived_credential(fx: ExecutorFixture) -> None:
    fx.tools.grant("t1", "crm.read")
    fx.secrets.grant_role("t1", "tool.crm_read")
    await run(fx, call("crm_read", "read"))
    credential = fx.invocations[-1].credential
    assert credential is not None
    assert await fx.secrets.bind(tenant_proto()).lease_status(credential.lease_id) == LeaseStatus.REVOKED, "用完即吊销"
    assert credential.secret.reveal() not in fx.telemetry_output.getvalue(), "凭据不进日志与 trace"


async def test_large_outputs_are_written_to_the_object_store(fx: ExecutorFixture) -> None:
    async def big(invocation: ToolInvocation) -> Any:
        return {"rows": ["x" * 100] * 2000}

    fx.behaviors["lookup_rate"] = big
    out = await run(fx, call("lookup_rate", "read"))
    assert set(out) >= {"summary", "ref", "size_bytes"} and out["ref"].startswith("obj://t1/"), "只回摘要与引用，路径带租户前缀"


async def test_a_transient_failure_of_a_read_tool_is_retryable(fx: ExecutorFixture) -> None:
    async def flaky(invocation: ToolInvocation) -> Any:
        raise TransientError("rate_limited", "限流")

    fx.behaviors["lookup_rate"] = flaky
    with pytest.raises(ApplicationError) as caught:
        await run(fx, call("lookup_rate", "read"))
    assert not caught.value.non_retryable
