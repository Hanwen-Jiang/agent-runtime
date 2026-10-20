"""开发实现：本地函数 ToolDriver。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from agentruntime_core.conformance.tool_driver import ToolDriverConformance
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider, ToolLookup
from agentruntime_core.enums import Effect
from agentruntime_core.errors import ErrorCategory, InvalidError, TransientError
from agentruntime_core.providers.tool_driver import ToolHandler, ToolInvocation, ToolSpec
from agentruntime_core.tenancy import TenantContext
from agentruntime_core.testing import FaultInjector


class Harness:
    def __init__(self) -> None:
        self.faults = FaultInjector()
        self.provider = LocalToolDriverProvider(faults=self.faults)

    async def advance(self, delta: timedelta) -> None:
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)

    def register(self, spec: ToolSpec, handler: ToolHandler, *, tenant_id: str | None = None) -> None:
        self.provider.register(spec, handler, tenant_id=tenant_id)

    def grant(self, tenant_id: str | None, permission: str) -> None:
        self.provider.grant(tenant_id, permission)


class TestLocalToolDriver(ToolDriverConformance):
    @pytest.fixture
    def harness(self) -> Harness:
        return Harness()


# --- 开发实现专有：lookup 交给外部系统（附录 K.4 的对账在进程重启后也能做）---

EXTERNAL_SPEC = ToolSpec(
    name="external_write",
    version="v1",
    description="写到外部系统",
    input_schema={"type": "object"},
    output_schema={"type": "object", "required": ["id"]},
    effect=Effect.WRITE_IRREVERSIBLE,
    permissions=frozenset(),
    timeout=timedelta(seconds=1),
    supports_idempotency_lookup=True,
)


class External:
    """外部系统的替身：按幂等键记下写入，进程内的幂等记录丢了它也还在。"""

    def __init__(self) -> None:
        self.writes: dict[str, dict] = {}
        self.queries: list[ToolLookup] = []

    async def handler(self, inv: ToolInvocation) -> dict:
        assert inv.idempotency_key is not None
        self.writes[inv.idempotency_key] = {"id": f"w{len(self.writes) + 1}"}
        return self.writes[inv.idempotency_key]

    async def lookup(self, query: ToolLookup) -> dict | None:
        self.queries.append(query)
        return self.writes.get(query.idempotency_key)


def tenant(tenant_id: str) -> TenantContext:
    return TenantContext(tenant_id=tenant_id)


async def test_lookup_asks_the_external_system_when_the_in_process_record_is_gone() -> None:
    external = External()
    before = LocalToolDriverProvider()
    before.register(EXTERNAL_SPEC, external.handler, lookup=external.lookup)
    written = await before.bind(tenant("t1")).invoke("external_write", "v1", {}, idempotency_key="k1")

    # “进程重启”：新的 provider 没有幂等记录，外部系统还记得。
    after = LocalToolDriverProvider()
    after.register(EXTERNAL_SPEC, external.handler, lookup=external.lookup)
    found = await after.bind(tenant("t1")).lookup("external_write", "v1", "k1")
    assert found is not None and found.output == written.output and found.replayed
    assert external.queries == [ToolLookup(tenant_id="t1", idempotency_key="k1")], "外部查询带的是客户端作用域的租户"
    assert await after.bind(tenant("t1")).lookup("external_write", "v1", "k2") is None


async def test_lookup_prefers_the_in_process_record() -> None:
    external = External()
    provider = LocalToolDriverProvider()
    provider.register(EXTERNAL_SPEC, external.handler, lookup=external.lookup)
    driver = provider.bind(tenant("t1"))
    await driver.invoke("external_write", "v1", {}, idempotency_key="k1")
    assert (await driver.lookup("external_write", "v1", "k1")) is not None
    assert external.queries == []


async def test_external_lookup_errors_and_timeouts_are_provider_errors() -> None:
    async def down(query: ToolLookup) -> dict | None:
        raise TransientError("unavailable", "外部系统暂时不可用")

    async def slow(query: ToolLookup) -> dict | None:
        await asyncio.sleep(5)
        return None

    async def bad(query: ToolLookup) -> dict | None:
        return {"no_id": True}

    for lookup, category, code in ((down, TransientError, "unavailable"), (slow, TransientError, "timeout"), (bad, InvalidError, "invalid_output")):
        provider = LocalToolDriverProvider()
        provider.register(EXTERNAL_SPEC, External().handler, lookup=lookup)
        with pytest.raises(category) as err:
            await provider.bind(tenant("t1")).lookup("external_write", "v1", "k1")
        assert err.value.code == code


def test_lookup_handler_requires_a_tool_that_declares_lookup_support() -> None:
    spec = ToolSpec(**{**EXTERNAL_SPEC.__dict__, "supports_idempotency_lookup": False})
    with pytest.raises(InvalidError):
        LocalToolDriverProvider().register(spec, External().handler, lookup=External().lookup)
