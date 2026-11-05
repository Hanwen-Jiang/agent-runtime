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


# --- 参数校验（TOOL-INT-FLOAT）：值是整数的浮点数照收；不合格时说明哪个参数、违反了什么，不回显参数的值 ---

STRICT_SPEC = ToolSpec(
    name="strict_args",
    version="v1",
    description="参数校验",
    input_schema={
        "type": "object",
        "required": ["hosts", "minutes"],
        "additionalProperties": False,
        "properties": {
            "hosts": {"type": "array", "minItems": 1, "items": {"type": "string", "maxLength": 20}},
            "minutes": {"type": "integer", "enum": [15, 30, 60]},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
            "reason": {"type": "string"},
        },
    },
    output_schema={"type": "object"},
    effect=Effect.READ,
    permissions=frozenset(),
    timeout=timedelta(seconds=1),
)


async def invoke_strict(arguments: dict) -> list[dict]:
    seen: list[dict] = []

    async def handler(inv: ToolInvocation) -> dict:
        seen.append(dict(inv.arguments))
        return {}

    provider = LocalToolDriverProvider()
    provider.register(STRICT_SPEC, handler)
    await provider.bind(tenant("t1")).invoke("strict_args", "v1", arguments)
    return seen


async def rejected(arguments: dict) -> str:
    with pytest.raises(InvalidError) as err:
        await invoke_strict(arguments)
    assert err.value.code == "invalid_arguments"
    return err.value.message


async def test_integral_floats_pass_integer_arguments() -> None:
    # 模型常把整数写成 15.0、8000.0（经 protobuf Struct 的数也都是浮点数）；JSON Schema 2020-12 把它们算作整数。
    seen = await invoke_strict({"hosts": ["example.com"], "minutes": 15.0, "port": 8000.0})
    assert seen == [{"hosts": ["example.com"], "minutes": 15.0, "port": 8000.0}]


@pytest.mark.parametrize("value", [15.5, True, "15"])
async def test_non_integral_numbers_booleans_and_strings_are_not_integers(value: object) -> None:
    assert "minutes" in await rejected({"hosts": ["a"], "minutes": value})


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"hosts": ["a"], "minutes": 10}, ["minutes", "15、30、60"]),
        ({"hosts": ["a"], "minutes": 15, "port": 70000}, ["port", "65535"]),
        ({"hosts": ["a"], "minutes": 15, "port": 0}, ["port", "1"]),
        ({"hosts": ["a"]}, ["缺少", "minutes"]),
        ({"hosts": [], "minutes": 15}, ["hosts", "1"]),
        ({"hosts": ["a"], "minutes": 15, "colour": "x"}, ["hosts、minutes、port、reason"]),
        ({"hosts": ["a", 7], "minutes": 15}, ["hosts/1", "string"]),
    ],
)
async def test_invalid_arguments_say_which_argument_and_what_is_allowed(arguments: dict, expected: list[str]) -> None:
    message = await rejected(arguments)
    assert message.startswith("参数不符合工具的 input_schema")
    for part in expected:
        assert part in message, message


async def test_only_the_first_problem_is_spelled_out_and_the_rest_are_counted() -> None:
    message = await rejected({"minutes": 10, "port": 0})
    assert "缺少" in message and "hosts" in message
    assert "另有 2 处" in message


@pytest.mark.parametrize(
    ("arguments", "secret"),
    [
        ({"hosts": ["x" * 30 + "TOPSECRETVALUE"], "minutes": 15}, "TOPSECRETVALUE"),
        ({"hosts": ["a"], "minutes": 15, "reason": 98765432123}, "98765432123"),
        ({"hosts": ["a"], "minutes": 4242}, "4242"),
        ({"hosts": ["a"], "minutes": 15, "sk-TOPSECRETKEY": "x"}, "TOPSECRETKEY"),
    ],
)
async def test_invalid_argument_messages_never_echo_what_the_caller_sent(arguments: dict, secret: str) -> None:
    # 参数是模型写的，可能带着它看到的凭据；说明只取自 schema，参数的值与没定义的参数名都不回显。
    message = await rejected(arguments)
    assert secret not in message
    assert len(message) <= 400
