"""ToolDriver 的一致性测试。语义见 agentruntime_core.providers.tool_driver。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import pytest

from agentruntime_core.conformance.common import TENANT_A_ID, TENANT_B_ID, Harness, StandardRules, expect_error, tenant_a, tenant_b
from agentruntime_core.enums import Effect
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.providers.secret_store import Credential
from agentruntime_core.providers.tool_driver import (
    ToolDriver,
    ToolDriverProvider,
    ToolHandler,
    ToolInvocation,
    ToolSpec,
)
from agentruntime_core.secrets import Secret


class ToolDriverHarness(Harness[ToolDriverProvider], Protocol):
    def register(self, spec: ToolSpec, handler: ToolHandler, *, tenant_id: str | None = None) -> None:
        """登记工具；tenant_id 为 None 表示平台工具，对所有作用域可见。"""
        ...

    def grant(self, tenant_id: str | None, permission: str) -> None:
        """把工具权限授予作用域；tenant_id 为 None 表示平台作用域。"""
        ...


def _object_schema(**props: str) -> dict[str, Any]:
    return {
        "type": "object",
        "required": list(props),
        "properties": {k: {"type": t} for k, t in props.items()},
        "additionalProperties": False,
    }


def _spec(name: str, effect: Effect, *, permissions: frozenset[str] = frozenset(), timeout: float = 5.0,
          lookup: bool = False, output: dict[str, Any] | None = None, **inputs: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        version="v1",
        description=f"conformance tool {name}",
        input_schema=_object_schema(**inputs),
        output_schema=output or {"type": "object"},
        effect=effect,
        permissions=permissions,
        timeout=timedelta(seconds=timeout),
        supports_idempotency_lookup=lookup,
    )


class ToolDriverConformance(StandardRules):
    probe_operation = "invoke"

    @pytest.fixture(autouse=True)
    def _tools(self, harness: ToolDriverHarness) -> None:
        executions: list[ToolInvocation] = []
        self.__dict__["executions"] = executions

        async def echo(call: ToolInvocation) -> Any:
            executions.append(call)
            return {"text": call.arguments["text"], "tenant_id": call.tenant_id}

        async def send_note(call: ToolInvocation) -> Any:
            executions.append(call)
            return {"receipt": f"note-{len(executions)}"}

        async def bad_output(call: ToolInvocation) -> Any:
            return {"unexpected": True}

        async def slow(call: ToolInvocation) -> Any:
            await asyncio.sleep(5)
            return {}

        async def whoami(call: ToolInvocation) -> Any:
            executions.append(call)
            secret = call.credential.secret.reveal() if call.credential else ""
            return {"has_credential": bool(secret)}

        async def noargs(call: ToolInvocation) -> Any:
            executions.append(call)
            return {}

        echo_out = {"type": "object", "required": ["text", "tenant_id"], "additionalProperties": False,
                    "properties": {"text": {"type": "string"}, "tenant_id": {"type": "string"}}}
        harness.register(_spec("echo", Effect.READ, output=echo_out, text="string"), echo)
        harness.register(_spec("send_note", Effect.WRITE_IRREVERSIBLE, lookup=True, to="string", body="string"), send_note)
        harness.register(_spec("update_row", Effect.WRITE_REVERSIBLE, row="string"), send_note)
        harness.register(_spec("bad_output", Effect.READ, output={"type": "object", "required": ["ok"]}), bad_output)
        harness.register(_spec("slow", Effect.READ, timeout=0.2), slow)
        harness.register(_spec("crm_read", Effect.READ, permissions=frozenset({"crm.read"})), noargs)
        harness.register(_spec("whoami", Effect.READ), whoami)
        harness.register(_spec("alpha_private", Effect.READ), noargs, tenant_id=TENANT_A_ID)

    @property
    def executions(self) -> list[ToolInvocation]:
        return self.__dict__["executions"]

    async def write_probe(self, client: ToolDriver, name: str) -> None:
        await client.invoke("send_note", "v1", {"to": "ops", "body": name}, idempotency_key=name)

    async def probe_visible(self, client: ToolDriver, name: str) -> bool:
        return await client.lookup("send_note", "v1", name) is not None

    @pytest.fixture
    def tools(self, harness: ToolDriverHarness) -> ToolDriver:
        return harness.provider.bind(tenant_a())

    async def test_invoke_read_tool(self, tools: ToolDriver) -> None:
        result = await tools.invoke("echo", "v1", {"text": "hi"})
        assert result.output == {"text": "hi", "tenant_id": TENANT_A_ID}
        assert result.replayed is False

    async def test_unregistered_tool_is_not_found(self, tools: ToolDriver) -> None:
        with expect_error(ErrorCategory.NOT_FOUND):
            await tools.invoke("no_such_tool", "v1", {})
        with expect_error(ErrorCategory.NOT_FOUND):
            await tools.describe("no_such_tool", "v1")
        with expect_error(ErrorCategory.NOT_FOUND):
            await tools.invoke("echo", "v9", {"text": "hi"})

    async def test_list_and_describe(self, harness: ToolDriverHarness, tools: ToolDriver) -> None:
        names_a = {spec.name for spec in await tools.list_tools()}
        names_b = {spec.name for spec in await harness.provider.bind(tenant_b()).list_tools()}
        assert {"echo", "send_note", "alpha_private"} <= names_a
        assert "echo" in names_b and "alpha_private" not in names_b
        spec = await tools.describe("send_note", "v1")
        assert spec.effect is Effect.WRITE_IRREVERSIBLE and spec.supports_idempotency_lookup

    async def test_tenant_private_tool_is_invisible_to_others(self, harness: ToolDriverHarness) -> None:
        b = harness.provider.bind(tenant_b())
        with expect_error(ErrorCategory.NOT_FOUND):
            await b.invoke("alpha_private", "v1", {})
        with expect_error(ErrorCategory.NOT_FOUND):
            await b.describe("alpha_private", "v1")

    async def test_arguments_must_match_input_schema(self, tools: ToolDriver) -> None:
        with expect_error(ErrorCategory.INVALID):
            await tools.invoke("echo", "v1", {"text": 42})
        with expect_error(ErrorCategory.INVALID):
            await tools.invoke("echo", "v1", {"text": "hi", "tenant_id": TENANT_B_ID})
        assert self.executions == []

    async def test_output_must_match_output_schema(self, tools: ToolDriver) -> None:
        with expect_error(ErrorCategory.INVALID, "invalid_output"):
            await tools.invoke("bad_output", "v1", {})

    async def test_missing_permission_is_denied(self, harness: ToolDriverHarness, tools: ToolDriver) -> None:
        with expect_error(ErrorCategory.DENIED):
            await tools.invoke("crm_read", "v1", {})
        harness.grant(TENANT_A_ID, "crm.read")
        await tools.invoke("crm_read", "v1", {})
        with expect_error(ErrorCategory.DENIED):
            await harness.provider.bind(tenant_b()).invoke("crm_read", "v1", {})

    @pytest.mark.parametrize("tool", ["send_note", "update_row"])
    async def test_write_without_idempotency_key_is_invalid(self, tools: ToolDriver, tool: str) -> None:
        args = {"to": "ops", "body": "x"} if tool == "send_note" else {"row": "r1"}
        with expect_error(ErrorCategory.INVALID):
            await tools.invoke(tool, "v1", args)
        assert self.executions == []

    async def test_same_idempotency_key_executes_once(self, tools: ToolDriver) -> None:
        args = {"to": "ops", "body": "refund report"}
        first = await tools.invoke("send_note", "v1", args, idempotency_key="idk-1")
        second = await tools.invoke("send_note", "v1", args, idempotency_key="idk-1")
        assert len(self.executions) == 1, "同一幂等键执行了不止一次"
        assert second.output == first.output
        assert first.replayed is False and second.replayed is True

    async def test_same_key_with_other_arguments_conflicts(self, tools: ToolDriver) -> None:
        await tools.invoke("send_note", "v1", {"to": "ops", "body": "a"}, idempotency_key="idk-1")
        with expect_error(ErrorCategory.CONFLICT):
            await tools.invoke("send_note", "v1", {"to": "ops", "body": "b"}, idempotency_key="idk-1")
        assert len(self.executions) == 1

    async def test_idempotency_records_are_isolated_per_tenant(self, harness: ToolDriverHarness, tools: ToolDriver) -> None:
        args = {"to": "ops", "body": "x"}
        await tools.invoke("send_note", "v1", args, idempotency_key="idk-1")
        result_b = await harness.provider.bind(tenant_b()).invoke("send_note", "v1", args, idempotency_key="idk-1")
        assert result_b.replayed is False
        assert [call.tenant_id for call in self.executions] == [TENANT_A_ID, TENANT_B_ID]

    async def test_lookup_reports_applied_writes(self, tools: ToolDriver) -> None:
        assert await tools.lookup("send_note", "v1", "idk-1") is None
        done = await tools.invoke("send_note", "v1", {"to": "ops", "body": "x"}, idempotency_key="idk-1")
        found = await tools.lookup("send_note", "v1", "idk-1")
        assert found is not None and found.output == done.output

    async def test_lookup_unsupported_is_invalid(self, tools: ToolDriver) -> None:
        with expect_error(ErrorCategory.INVALID, "lookup_unsupported"):
            await tools.lookup("update_row", "v1", "idk-1")

    async def test_timeout_is_transient(self, tools: ToolDriver) -> None:
        with expect_error(ErrorCategory.TRANSIENT, "timeout"):
            await tools.invoke("slow", "v1", {})

    async def test_handler_sees_bound_tenant_and_credential(self, tools: ToolDriver) -> None:
        credential = Credential(lease_id="lease-1", role="crm-reader", secret=Secret("short-lived"),
                                expires_at=datetime(2030, 1, 1, tzinfo=UTC))
        result = await tools.invoke("whoami", "v1", {}, credential=credential)
        assert result.output == {"has_credential": True}
        assert self.executions[-1].tenant_id == TENANT_A_ID
        assert self.executions[-1].credential is credential

    async def test_handler_sees_the_bound_workspace(self, tools: ToolDriver) -> None:
        # T-10：平台内工具按租户与 workspace 划分工作区；workspace 同租户一样来自绑定的作用域，参数改变不了它。
        await tools.invoke("whoami", "v1", {})
        assert self.executions[-1].workspace_id == "w-1"
