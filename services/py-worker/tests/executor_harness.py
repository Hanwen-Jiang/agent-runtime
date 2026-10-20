"""Node Executor 测试的支架：用 S-02 的开发实现拼出 ExecutorDeps，登记一组测试工具，计数每个工具真正执行了几次。

工具的名字与版本就是 program 节点的 template（例如 lookup_rate@v1）。
"""

from __future__ import annotations

import asyncio
import io
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from agentruntime_core.dev.cache import InMemoryCacheProvider
from agentruntime_core.dev.human_channel import CliHumanChannelProvider
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.sandbox import LocalSubprocessSandboxProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.enums import Effect
from agentruntime_core.errors import TransientError
from agentruntime_core.providers.human_channel import Reply
from agentruntime_core.providers.tool_driver import ToolInvocation, ToolResult, ToolSpec

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

from agentruntime_worker.activities.types import GateRecord, NodeInput, Tenant
from agentruntime_worker.executor import keys
from agentruntime_worker.executor.node_executor import ExecutorConfig, ExecutorDeps, NodeExecutor
from agentruntime_worker.executor.schemas import DictSchemaResolver

from .support import SESSION_ID

TENANT = Tenant(tenant_id="t1", user_id="u1", session_id=SESSION_ID)
OTHER_TENANT = Tenant(tenant_id="t2", user_id="u2", session_id=SESSION_ID)
TASK_ID = "tsk_01J" + "0" * 22 + "E"

RATE_SCHEMA = {"type": "object", "required": ["rate"], "properties": {"rate": {"type": "number"}}}
REGISTERED_SCHEMAS = {"region_rate_v1": RATE_SCHEMA}


class _Sink:
    def __init__(self) -> None:
        self.replies: list[Reply] = []

    async def deliver(self, reply: Reply) -> None:
        self.replies.append(reply)


class ScriptedGateway:
    """Model Gateway 的替身：依次返回脚本里的响应，记下每个请求。"""

    def __init__(self) -> None:
        self.requests: list[mg.GenerateRequest] = []
        self.responses: list[mg.GenerateResponse] = []
        self.embed_requests: list[mg.EmbedRequest] = []

    def reply(self, *results: Any, rationale: str = "模型的推理过程") -> None:
        from google.protobuf import json_format, struct_pb2

        for r in results:
            value = struct_pb2.Value()
            json_format.ParseDict(r, value)
            self.responses.append(
                mg.GenerateResponse(
                    result=value,
                    rationale=rationale,
                    finish_reason=mg.FINISH_REASON_STOP,
                    model=mg.ModelChoice(model_profile_id="mock/model", tier="strong@medium", vendor="mock"),
                )
            )

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        self.embed_requests.append(request)
        return mg.EmbedResponse()


Handler = Callable[[ToolInvocation], Awaitable[Any]]


@dataclass
class ExecutorFixture:
    root: Path
    tools: LocalToolDriverProvider
    objects: LocalDiskObjectStoreProvider
    secrets: LocalFileSecretStoreProvider
    cache: InMemoryCacheProvider
    humans: CliHumanChannelProvider
    human_output: io.StringIO
    telemetry: ConsoleTelemetryProvider
    telemetry_output: io.StringIO
    models: ScriptedGateway
    sandbox: LocalSubprocessSandboxProvider
    executor: NodeExecutor
    calls: Counter[str] = field(default_factory=Counter)
    invocations: list[ToolInvocation] = field(default_factory=list)
    # 工具名 → 行为；行为返回输出或抛错。缺省回显参数。
    behaviors: dict[str, Handler] = field(default_factory=dict)

    def register(
        self,
        name: str,
        effect: Effect,
        *,
        input_schema: Mapping[str, Any] | None = None,
        output_schema: Mapping[str, Any] | None = None,
        lookup: bool = False,
        permissions: frozenset[str] = frozenset(),
        timeout: float = 5,
    ) -> None:
        async def handler(invocation: ToolInvocation) -> Any:
            self.calls[name] += 1
            self.invocations.append(invocation)
            behave = self.behaviors.get(name)
            if behave is None:
                return {"echo": dict(invocation.arguments)}
            return await behave(invocation)

        self.tools.register(
            ToolSpec(
                name=name,
                version="v1",
                description=name,
                input_schema=input_schema or {"type": "object"},
                output_schema=output_schema or {"type": "object"},
                effect=effect,
                permissions=permissions,
                timeout=timedelta(seconds=timeout),
                supports_idempotency_lookup=lookup,
            ),
            handler,
        )


def make_fixture(root: Path, *, config: ExecutorConfig | None = None, tools: Any = None) -> ExecutorFixture:
    telemetry_output = io.StringIO()
    human_output = io.StringIO()
    tool_provider = LocalToolDriverProvider()
    fixture = ExecutorFixture(
        root=root,
        tools=tool_provider,
        objects=LocalDiskObjectStoreProvider(root / "objects", signing_key=b"executor-test-key"),
        secrets=LocalFileSecretStoreProvider(root / "secrets.json"),
        cache=InMemoryCacheProvider(),
        humans=CliHumanChannelProvider(_Sink(), output=human_output),
        human_output=human_output,
        telemetry=ConsoleTelemetryProvider(stream=telemetry_output),
        telemetry_output=telemetry_output,
        models=ScriptedGateway(),
        sandbox=LocalSubprocessSandboxProvider(root / "sandbox"),
        executor=None,  # type: ignore[arg-type]
    )
    fixture.executor = NodeExecutor(
        ExecutorDeps(
            tools=tools(fixture.tools) if tools else fixture.tools,
            objects=fixture.objects,
            secrets=fixture.secrets,
            cache=fixture.cache,
            humans=fixture.humans,
            telemetry=fixture.telemetry,
            models=fixture.models,
            schemas=DictSchemaResolver(REGISTERED_SCHEMAS),
            sandbox=fixture.sandbox,
        ),
        config or ExecutorConfig(),
    )
    fixture.register("lookup_rate", Effect.READ, output_schema={"type": "object"})
    fixture.register("compute", Effect.NONE)
    fixture.register("update_crm", Effect.WRITE_REVERSIBLE, lookup=True)
    fixture.register("pay", Effect.WRITE_IRREVERSIBLE, lookup=True)
    fixture.register("send_mail", Effect.WRITE_IRREVERSIBLE, lookup=False)
    fixture.register("crm_read", Effect.READ, permissions=frozenset({"crm.read"}))
    fixture.register("strict_output", Effect.READ, output_schema=RATE_SCHEMA)
    fixture.register(
        "strict_input",
        Effect.READ,
        input_schema={"type": "object", "required": ["region"], "properties": {"region": {"type": "string"}}},
    )
    return fixture


def program(
    node_id: str,
    tool: str,
    *,
    effect: str = "read",
    inputs: dict[str, Any] | None = None,
    task_id: str = TASK_ID,
    tenant: Tenant = TENANT,
    with_key: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "kind": "atomic",
        "template": f"{tool}@v1",
        "executor": "program",
        "effect": effect,
        "role": "act" if effect.startswith("write") else "fetch",
        "inputs": inputs if inputs is not None else {"q": 1},
        **extra,
    }
    if effect.startswith("write") and with_key:
        node["idempotency_key"] = keys.idempotency_key(tenant.tenant_id, task_id, node_id, node["inputs"])
    return node


def model(node_id: str, *, inputs: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": "atomic",
        "template": "summarize@v2",
        "executor": "model",
        "output": "generate",
        "effect": "none",
        "role": "transform",
        "inputs": inputs if inputs is not None else {"text": "华东退款率上升"},
        **extra,
    }


def node_input(node: dict[str, Any], *, gate: GateRecord | None = None, tenant: Tenant = TENANT, task_id: str = TASK_ID, injections: list[dict[str, Any]] | None = None) -> NodeInput:
    return NodeInput(tenant=tenant, task_id=task_id, round_seq=1, node=node, gate=gate, injections=injections or [])


def approved(node_id: str, user: str = "u7") -> GateRecord:
    return GateRecord(gate_node=f"{node_id}.1", option="approve", approved_by=user)


class ResponseLost:
    """ToolDriver 的包装：调用真正执行（外部已生效）之后，响应在半路丢了，调用方看到超时。"""

    def __init__(self, inner: Any, *, lose: Callable[[str], bool]) -> None:
        self._inner = inner
        self._lose = lose

    def bind(self, tenant: Any) -> Any:
        return _LossyDriver(self._inner.bind(tenant), self._lose)

    def bind_platform(self, purpose: str) -> Any:
        return _LossyDriver(self._inner.bind_platform(purpose), self._lose)

    async def aclose(self) -> None:
        await self._inner.aclose()


class _LossyDriver:
    def __init__(self, inner: Any, lose: Callable[[str], bool]) -> None:
        self._inner = inner
        self._lose = lose

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def invoke(self, name: str, version: str, arguments: Mapping[str, Any], **kwargs: Any) -> ToolResult:
        result = await self._inner.invoke(name, version, arguments, **kwargs)
        if self._lose(name):
            raise TransientError("timeout", "工具调用超时（响应丢失）")
        return result


async def never_returns(_: ToolInvocation) -> Any:
    await asyncio.sleep(3600)


class ExternalLedger:
    """外部系统的替身（计划 S-07）：写入按幂等键记下，进程内的幂等记录丢了它也还在；不按幂等键去重，只支持查询。

    lose_response：写入生效后响应丢失（调用方看到超时）。lookup_failures：接下来几次查询报 transient。
    """

    def __init__(self, *, lose_response: bool = False) -> None:
        self.applied: list[str] = []
        self.records: dict[str, dict[str, Any]] = {}
        self.lookup_failures = 0
        self.lose_response = lose_response

    async def handler(self, invocation: ToolInvocation) -> dict[str, Any]:
        assert invocation.idempotency_key is not None
        self.applied.append(invocation.idempotency_key)
        self.records.setdefault(invocation.idempotency_key, {"txn": f"txn-{len(self.applied)}"})
        if self.lose_response:
            raise TransientError("timeout", "外部已生效，响应丢失")
        return {"txn": f"txn-{len(self.applied)}"}

    async def lookup(self, query: Any) -> dict[str, Any] | None:
        if self.lookup_failures:
            self.lookup_failures -= 1
            raise TransientError("unavailable", "外部查询接口暂时不可用")
        return self.records.get(query.idempotency_key)


def register_external(fx: ExecutorFixture, name: str, effect: Effect, ledger: ExternalLedger) -> None:
    """登记一个背后是 ledger 的工具，按幂等键的查询交给 ledger。"""
    spec = ToolSpec(
        name=name,
        version="v1",
        description=name,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        effect=effect,
        permissions=frozenset(),
        timeout=timedelta(seconds=5),
        supports_idempotency_lookup=True,
    )
    fx.tools.register(spec, ledger.handler, lookup=ledger.lookup)


class SwitchableTools:
    """ToolDriver 工厂的包装：测试可以随时换上一层故障（wrap），之后的 bind 都经过它。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.wrap: Callable[[Any], Any] | None = None

    def bind(self, tenant: Any) -> Any:
        return (self.wrap(self.inner) if self.wrap else self.inner).bind(tenant)

    def bind_platform(self, purpose: str) -> Any:
        return (self.wrap(self.inner) if self.wrap else self.inner).bind_platform(purpose)

    async def aclose(self) -> None:
        await self.inner.aclose()


class HangAfterEffect:
    """第一次调用真正执行（外部已生效）之后一直不返回，模拟“已执行、结果未写入历史”时 Worker 失联。"""

    def __init__(self, inner: Any, tools: tuple[str, ...]) -> None:
        self._inner = inner
        self._tools = tools
        self.hung: list[str] = []

    def __call__(self, inner: Any) -> Any:
        return self

    def bind(self, tenant: Any) -> Any:
        return _HangingDriver(self._inner.bind(tenant), self)

    def bind_platform(self, purpose: str) -> Any:
        return _HangingDriver(self._inner.bind_platform(purpose), self)

    async def aclose(self) -> None:
        await self._inner.aclose()


class _HangingDriver:
    def __init__(self, inner: Any, owner: HangAfterEffect) -> None:
        self._inner = inner
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def invoke(self, name: str, version: str, arguments: Mapping[str, Any], **kwargs: Any) -> ToolResult:
        result = await self._inner.invoke(name, version, arguments, **kwargs)
        if name in self._owner._tools and name not in self._owner.hung:
            self._owner.hung.append(name)
            await asyncio.sleep(3600)
        return result
