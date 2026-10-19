"""Telemetry 的一致性测试。语义见 agentruntime_core.providers.telemetry。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import pytest

from agentruntime.common.v1 import tenant_pb2

from agentruntime_core.conformance.common import (
    TENANT_A_ID,
    TENANT_B_ID,
    BindingRules,
    Harness,
    expect_error,
    inject_or_skip,
    tenant,
    tenant_a,
)
from agentruntime_core.errors import ErrorCategory, ProviderError
from agentruntime_core.providers.telemetry import REDACTED, LogLevel, Telemetry, TelemetryProvider
from agentruntime_core.secrets import Secret

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"
SECRET_VALUE = "sk-live-conformance-123"


@dataclass(frozen=True)
class ExportedSpan:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str
    attributes: Mapping[str, Any]
    # ok 或 error。
    status: str
    events: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExportedMetric:
    name: str
    # counter 或 histogram。
    kind: str
    value: float
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExportedLog:
    level: str
    message: str
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Exported:
    spans: tuple[ExportedSpan, ...]
    metrics: tuple[ExportedMetric, ...]
    logs: tuple[ExportedLog, ...]


class TelemetryHarness(Harness[TelemetryProvider], Protocol):
    def exported(self) -> Exported:
        """flush 之后已导出的全部记录。故障注入的操作名 export 表示导出失败。"""
        ...


def _all_rendered(exported: Exported) -> str:
    return repr(exported)


class TelemetryConformance(BindingRules):
    @pytest.fixture
    def telemetry(self, harness: TelemetryHarness) -> Telemetry:
        return harness.provider.bind(tenant_a())

    async def test_records_carry_tenant_attributes(self, harness: TelemetryHarness) -> None:
        context = tenant(TENANT_A_ID, user_id="u-alice", workspace_id="w-1", session_id="ses_01JA8Q2N4H6K8M0P2R4T6V8X0Z")
        telemetry = harness.provider.bind(context)
        async with telemetry.start_span("runtime.round"):
            await telemetry.count("rounds")
            await telemetry.log(LogLevel.INFO, "round started")
        await telemetry.flush()
        exported = harness.exported()
        expected = {"tenant_id": TENANT_A_ID, "user_id": "u-alice", "workspace_id": "w-1",
                    "session_id": "ses_01JA8Q2N4H6K8M0P2R4T6V8X0Z"}
        for record in (exported.spans[0], exported.metrics[0], exported.logs[0]):
            for key, value in expected.items():
                assert record.attributes.get(key) == value, f"{type(record).__name__} 缺少 {key}"

    async def test_tenant_attributes_cannot_be_forged(self, harness: TelemetryHarness, telemetry: Telemetry) -> None:
        forged = {"tenant_id": TENANT_B_ID, "user_id": "u-mallory"}
        async with telemetry.start_span("node", attributes=forged) as span:
            span.set_attributes({"tenant_id": TENANT_B_ID})
        await telemetry.count("calls", attributes=forged)
        await telemetry.log(LogLevel.INFO, "x", attributes=forged)
        await telemetry.flush()
        exported = harness.exported()
        for record in (exported.spans[0], exported.metrics[0], exported.logs[0]):
            assert record.attributes["tenant_id"] == TENANT_A_ID
            assert record.attributes["user_id"] == "u-alice"

    async def test_platform_scope_has_no_tenant_attributes(self, harness: TelemetryHarness) -> None:
        telemetry = harness.provider.bind_platform("conformance")
        async with telemetry.start_span("worker.startup"):
            pass
        await telemetry.flush()
        span = harness.exported().spans[0]
        assert span.attributes.get("scope") == "platform"
        assert "tenant_id" not in span.attributes

    async def test_root_span_joins_incoming_trace(self, harness: TelemetryHarness) -> None:
        context = tenant(TENANT_A_ID, trace=tenant_pb2.TraceContext(traceparent=TRACEPARENT))
        telemetry = harness.provider.bind(context)
        async with telemetry.start_span("invoke_workflow") as span:
            assert span.trace_id == TRACE_ID
        await telemetry.flush()
        assert harness.exported().spans[0].trace_id == TRACE_ID

    async def test_nested_spans_form_a_tree(self, harness: TelemetryHarness, telemetry: Telemetry) -> None:
        async with telemetry.start_span("runtime.round") as parent:
            async with telemetry.start_span("execute_tool") as child:
                assert child.trace_id == parent.trace_id
        await telemetry.flush()
        spans = {s.name: s for s in harness.exported().spans}
        assert spans["execute_tool"].parent_span_id == spans["runtime.round"].span_id
        assert spans["execute_tool"].trace_id == spans["runtime.round"].trace_id

    async def test_exception_marks_span_and_propagates(self, harness: TelemetryHarness, telemetry: Telemetry) -> None:
        class Boom(Exception):
            pass

        with pytest.raises(Boom):
            async with telemetry.start_span("node"):
                raise Boom("tool exploded")
        await telemetry.flush()
        span = harness.exported().spans[0]
        assert span.status == "error"
        assert "Boom" in repr(span.attributes) + repr(span.events)

    @pytest.mark.parametrize(
        "key", ["password", "db_secret", "access_token", "api_key", "x-apikey", "Authorization", "credential", "cookie"]
    )
    async def test_sensitive_attribute_keys_are_redacted(self, harness: TelemetryHarness, telemetry: Telemetry, key: str) -> None:
        async with telemetry.start_span("node", attributes={key: SECRET_VALUE}):
            pass
        await telemetry.flush()
        span = harness.exported().spans[0]
        assert span.attributes[key] == REDACTED
        assert SECRET_VALUE not in _all_rendered(harness.exported())

    async def test_secret_values_are_redacted_everywhere(self, harness: TelemetryHarness, telemetry: Telemetry) -> None:
        secret = Secret(SECRET_VALUE)
        async with telemetry.start_span("node", attributes={"tool_input": secret}) as span:
            span.add_event("called", {"arg": secret})
            span.set_attributes({"later": secret})
        await telemetry.count("calls", attributes={"key": secret})
        await telemetry.log(LogLevel.WARNING, f"calling with {secret}", attributes={"arg": secret})
        await telemetry.flush()
        exported = harness.exported()
        assert SECRET_VALUE not in _all_rendered(exported)
        assert exported.spans[0].attributes["tool_input"] == REDACTED
        assert exported.logs[0].attributes["arg"] == REDACTED

    async def test_metrics_record_values(self, harness: TelemetryHarness, telemetry: Telemetry) -> None:
        await telemetry.count("node.calls", 2, attributes={"role": "fetch"})
        await telemetry.observe("node.latency_ms", 12.5)
        await telemetry.flush()
        metrics = {m.name: m for m in harness.exported().metrics}
        assert metrics["node.calls"].kind == "counter" and metrics["node.calls"].value == 2
        assert metrics["node.calls"].attributes["role"] == "fetch"
        assert metrics["node.latency_ms"].kind == "histogram" and metrics["node.latency_ms"].value == 12.5

    async def test_empty_names_are_invalid(self, telemetry: Telemetry) -> None:
        with expect_error(ErrorCategory.INVALID):
            async with telemetry.start_span(""):
                pass
        with expect_error(ErrorCategory.INVALID):
            await telemetry.count("")

    async def test_export_failure_does_not_break_callers(self, harness: TelemetryHarness, telemetry: Telemetry) -> None:
        inject_or_skip(harness, "export", ErrorCategory.TRANSIENT)
        async with telemetry.start_span("node"):
            await telemetry.count("calls")
            await telemetry.log(LogLevel.INFO, "still fine")
        try:
            await telemetry.flush()
        except ProviderError as error:
            assert error.retryable, "导出失败只能报 transient"
