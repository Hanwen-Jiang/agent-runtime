"""控制台 Telemetry（开发实现）。语义见 agentruntime_core.providers.telemetry。

flush 时把记录按 JSON 逐行写到 stream，并保留在 exported_spans、exported_metrics、exported_logs 里供测试检查。
所有属性在记录时就已脱敏，控制台与内存里都不会出现明文。
"""

from __future__ import annotations

import contextvars
import json
import re
import secrets
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, TextIO

from agentruntime_core.clock import Clock, SystemClock
from agentruntime_core.errors import InvalidError
from agentruntime_core.providers.telemetry import Attributes, LogLevel
from agentruntime_core.telemetry_rules import sanitize
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector

_TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}")


@dataclass
class SpanRecord:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str
    attributes: dict[str, Any]
    status: str
    events: tuple[str, ...] = ()


@dataclass
class MetricRecord:
    name: str
    kind: str
    value: float
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class LogRecord:
    level: str
    message: str
    attributes: dict[str, Any] = field(default_factory=dict)


class _Span:
    def __init__(self, record: SpanRecord) -> None:
        self._record = record
        self._events: list[str] = []

    @property
    def trace_id(self) -> str:
        return self._record.trace_id

    @property
    def span_id(self) -> str:
        return self._record.span_id

    def set_attributes(self, attributes: Attributes) -> None:
        self._record.attributes.update(sanitize(attributes))

    def add_event(self, name: str, attributes: Attributes | None = None) -> None:
        clean = sanitize(attributes)
        self._events.append(name if not clean else f"{name} {json.dumps(clean, ensure_ascii=False, default=str)}")

    def record_error(self, error: BaseException) -> None:
        self._record.status = "error"
        self._record.attributes["error.type"] = type(error).__name__
        self._events.append(f"exception {type(error).__name__}")


class ConsoleTelemetryProvider:
    def __init__(self, *, stream: TextIO | None = None, clock: Clock | None = None, faults: FaultInjector | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._clock = clock or SystemClock()
        self._faults = faults or FaultInjector()
        self._pending: list[SpanRecord | MetricRecord | LogRecord] = []
        self._current: contextvars.ContextVar[SpanRecord | None] = contextvars.ContextVar("span", default=None)
        self.exported_spans: list[SpanRecord] = []
        self.exported_metrics: list[MetricRecord] = []
        self.exported_logs: list[LogRecord] = []

    def bind(self, tenant: TenantContext) -> ConsoleTelemetry:
        return ConsoleTelemetry(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> ConsoleTelemetry:
        return ConsoleTelemetry(self, platform_scope(purpose))

    async def aclose(self) -> None:
        return None

    def _flush(self) -> None:
        self._faults.check("export")
        for record in self._pending:
            kind = type(record).__name__.removesuffix("Record").lower()
            self._stream.write(json.dumps({"kind": kind, **asdict(record)}, ensure_ascii=False, default=str) + "\n")
            if isinstance(record, SpanRecord):
                self.exported_spans.append(record)
            elif isinstance(record, MetricRecord):
                self.exported_metrics.append(record)
            else:
                self.exported_logs.append(record)
        self._pending.clear()


class ConsoleTelemetry:
    def __init__(self, provider: ConsoleTelemetryProvider, scope: Scope) -> None:
        self._provider = provider
        self._scope = scope
        if scope.is_platform:
            self._scope_attributes: dict[str, Any] = {"scope": "platform"}
        else:
            self._scope_attributes = {
                k: v
                for k, v in {
                    "tenant_id": scope.tenant_id,
                    "user_id": scope.user_id,
                    "workspace_id": scope.workspace_id,
                    "session_id": scope.session_id,
                }.items()
                if v
            }

    def _attributes(self, attributes: Attributes | None) -> dict[str, Any]:
        return {**sanitize(attributes), **self._scope_attributes}

    @staticmethod
    def _require_name(name: str) -> None:
        if not isinstance(name, str) or not name:
            raise InvalidError("invalid_argument", "名称不能为空")

    def start_span(self, name: str, *, attributes: Attributes | None = None) -> Any:
        self._require_name(name)
        return self._span(name, attributes)

    @asynccontextmanager
    async def _span(self, name: str, attributes: Attributes | None) -> AsyncIterator[_Span]:
        parent = self._provider._current.get()
        if parent is not None:
            trace_id, parent_id = parent.trace_id, parent.span_id
        else:
            match = _TRACEPARENT.fullmatch(self._scope.traceparent) if self._scope.traceparent else None
            trace_id, parent_id = (match.group(1), match.group(2)) if match else (secrets.token_hex(16), "")
        record = SpanRecord(
            name=name,
            trace_id=trace_id,
            span_id=secrets.token_hex(8),
            parent_span_id=parent_id,
            attributes=self._attributes(attributes),
            status="ok",
        )
        span = _Span(record)
        token = self._provider._current.set(record)
        try:
            yield span
        except BaseException as error:
            span.record_error(error)
            raise
        finally:
            self._provider._current.reset(token)
            record.attributes.update(self._scope_attributes)  # set_attributes 不能覆盖租户属性
            record.events = tuple(span._events)
            self._provider._pending.append(record)

    async def count(self, name: str, value: int = 1, *, attributes: Attributes | None = None) -> None:
        self._require_name(name)
        self._provider._pending.append(MetricRecord(name=name, kind="counter", value=value, attributes=self._attributes(attributes)))

    async def observe(self, name: str, value: float, *, attributes: Attributes | None = None) -> None:
        self._require_name(name)
        self._provider._pending.append(MetricRecord(name=name, kind="histogram", value=value, attributes=self._attributes(attributes)))

    async def log(self, level: LogLevel, message: str, *, attributes: Attributes | None = None) -> None:
        self._provider._pending.append(LogRecord(level=str(level), message=str(message), attributes=self._attributes(attributes)))

    async def flush(self) -> None:
        self._provider._flush()
