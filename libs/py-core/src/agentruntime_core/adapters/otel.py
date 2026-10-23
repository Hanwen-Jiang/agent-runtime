"""OpenTelemetry 实现的 Telemetry（设计书 4.13；T-15，细化见 docs/plan/deploy-telemetry.md）。语义见 agentruntime_core.providers.telemetry。

- span、指标、日志都经 OTLP/HTTP（protobuf）发到同一个 Collector：<endpoint>/v1/traces、/v1/metrics、/v1/logs。
- 资源属性只有服务名、版本与环境；租户属性由绑定加在每条记录上（一个进程服务多个租户）。属性规则与控制台实现共用
  agentruntime_core.telemetry_rules。
- 打点只进 SDK 的缓冲，后台按批导出；flush 强制导出一次，等待不超过导出超时（另加一点余量）。期间任何一次导出失败
  （含后台的），flush 报 transient；start_span、count、observe、log 只在参数不合法时报 invalid。
- 指标按 delta 时间性导出：counter 是 Int64 counter，observe 是 histogram。
- 异常只记类型（span 状态为 error，属性 error.type 与事件 exception），不记消息与调用栈：消息里可能有凭据。
- 不设置 OpenTelemetry 的全局 Provider；嵌套关系用 OpenTelemetry 的上下文（contextvars）。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, TextIO

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExportResult
from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider, ObservableCounter, ObservableGauge, ObservableUpDownCounter, UpDownCounter
from opentelemetry.sdk.metrics.export import AggregationTemporality, MetricExportResult, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.errors import InvalidError, TransientError
from agentruntime_core.providers.telemetry import Attributes, LogLevel, TelemetryProvider
from agentruntime_core.telemetry_rules import sanitize
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope

ENV_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
ENV_SERVICE_VERSION = "OTEL_SERVICE_VERSION"
ENV_ENVIRONMENT = "OTEL_DEPLOYMENT_ENVIRONMENT"
DEFAULT_EXPORT_TIMEOUT = timedelta(seconds=2)
# 后台导出的间隔：调用方多数在每个 activity 结束时 flush，后台只兜底。
SCHEDULE_DELAY_MS = 5000
METRIC_INTERVAL_MS = 60000
# OpenTelemetry 的仪表名规则。
_INSTRUMENT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_./-]{0,254}$")
_SEVERITY = {
    LogLevel.DEBUG: SeverityNumber.DEBUG,
    LogLevel.INFO: SeverityNumber.INFO,
    LogLevel.WARNING: SeverityNumber.WARN,
    LogLevel.ERROR: SeverityNumber.ERROR,
}
_DELTA = {
    Counter: AggregationTemporality.DELTA,
    UpDownCounter: AggregationTemporality.CUMULATIVE,
    Histogram: AggregationTemporality.DELTA,
    ObservableCounter: AggregationTemporality.DELTA,
    ObservableUpDownCounter: AggregationTemporality.CUMULATIVE,
    ObservableGauge: AggregationTemporality.CUMULATIVE,
}


class _Failures:
    """记下上次 flush 以来是否有导出失败（后台线程与 flush 都会导出）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failed = False

    def mark(self) -> None:
        with self._lock:
            self._failed = True

    def take(self) -> bool:
        with self._lock:
            failed, self._failed = self._failed, False
            return failed


class _SpanExporter(OTLPSpanExporter):
    def __init__(self, failures: _Failures, **kw: Any) -> None:
        super().__init__(**kw)
        self._failures = failures

    def export(self, spans: Any) -> SpanExportResult:
        result = super().export(spans)
        if result is not SpanExportResult.SUCCESS:
            self._failures.mark()
        return result


class _MetricExporter(OTLPMetricExporter):
    def __init__(self, failures: _Failures, **kw: Any) -> None:
        super().__init__(**kw)
        self._failures = failures

    def export(self, metrics_data: Any, timeout_millis: float = 10_000, **kw: Any) -> MetricExportResult:
        result = super().export(metrics_data, timeout_millis=timeout_millis, **kw)
        if result is not MetricExportResult.SUCCESS:
            self._failures.mark()
        return result


class _LogExporter(OTLPLogExporter):
    def __init__(self, failures: _Failures, **kw: Any) -> None:
        super().__init__(**kw)
        self._failures = failures

    def export(self, batch: Any) -> LogRecordExportResult:
        result = super().export(batch)
        if result is not LogRecordExportResult.SUCCESS:
            self._failures.mark()
        return result


class OtelTelemetryProvider:
    def __init__(
        self,
        endpoint: str,
        *,
        service_name: str,
        service_version: str = "",
        environment: str = "",
        export_timeout: timedelta = DEFAULT_EXPORT_TIMEOUT,
    ) -> None:
        if not endpoint.strip():
            raise InvalidError("invalid_argument", "Collector 的地址不能为空")
        if not service_name:
            raise InvalidError("invalid_argument", "服务名不能为空")
        self.endpoint = endpoint.strip().rstrip("/")
        self.service_name = service_name
        self.service_version = service_version
        self.environment = environment
        self.export_timeout = export_timeout
        attributes = {"service.name": service_name}
        if service_version:
            attributes["service.version"] = service_version
        if environment:
            attributes["deployment.environment"] = environment
        resource = Resource.create(attributes)
        timeout_s = export_timeout.total_seconds()
        self._failures = _Failures()

        self._tracer_provider = TracerProvider(resource=resource, sampler=ALWAYS_ON, shutdown_on_exit=False)
        self._tracer_provider.add_span_processor(
            BatchSpanProcessor(
                _SpanExporter(self._failures, endpoint=f"{self.endpoint}/v1/traces", timeout=timeout_s),
                schedule_delay_millis=SCHEDULE_DELAY_MS,
                export_timeout_millis=timeout_s * 1000,
            )
        )
        self._tracer = self._tracer_provider.get_tracer("agentruntime")

        reader = PeriodicExportingMetricReader(
            _MetricExporter(self._failures, endpoint=f"{self.endpoint}/v1/metrics", timeout=timeout_s, preferred_temporality=_DELTA),
            export_interval_millis=METRIC_INTERVAL_MS,
            export_timeout_millis=timeout_s * 1000,
        )
        self._meter_provider = MeterProvider(resource=resource, metric_readers=[reader], shutdown_on_exit=False)
        self._meter = self._meter_provider.get_meter("agentruntime")
        self._instruments: dict[tuple[str, str], Any] = {}
        self._instruments_lock = threading.Lock()

        self._logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
        self._logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                _LogExporter(self._failures, endpoint=f"{self.endpoint}/v1/logs", timeout=timeout_s),
                schedule_delay_millis=SCHEDULE_DELAY_MS,
                export_timeout_millis=timeout_s * 1000,
            )
        )
        self._logger = self._logger_provider.get_logger("agentruntime")
        self._closed = False

    def __repr__(self) -> str:
        return f"OtelTelemetryProvider({self.endpoint!r}, service_name={self.service_name!r})"

    def bind(self, tenant: TenantContext) -> OtelTelemetry:
        return OtelTelemetry(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> OtelTelemetry:
        return OtelTelemetry(self, platform_scope(purpose))

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        timeout_ms = int(self.export_timeout.total_seconds() * 1000)

        def shutdown() -> None:
            self._tracer_provider.shutdown()
            self._meter_provider.shutdown(timeout_millis=timeout_ms)
            self._logger_provider.shutdown()

        await asyncio.to_thread(shutdown)

    # --- 供绑定的客户端使用 ---

    def _instrument(self, kind: str, name: str) -> Any:
        if not _INSTRUMENT_NAME.fullmatch(name):
            raise InvalidError("invalid_argument", f"指标名不合法：{name!r}")
        with self._instruments_lock:
            key = (kind, name)
            if key not in self._instruments:
                create = self._meter.create_counter if kind == "counter" else self._meter.create_histogram
                self._instruments[key] = create(name)
            return self._instruments[key]

    async def _flush(self) -> None:
        timeout = self.export_timeout.total_seconds()

        def force(flush: Any) -> bool:
            return bool(flush(timeout_millis=int(timeout * 1000)))

        started = time.monotonic()
        jobs = [
            asyncio.to_thread(force, self._tracer_provider.force_flush),
            asyncio.to_thread(force, self._meter_provider.force_flush),
            asyncio.to_thread(force, self._logger_provider.force_flush),
        ]
        try:
            done = await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), timeout=timeout + 0.5)
        except TimeoutError:
            self._failures.take()
            raise TransientError("export_timeout", f"导出到 Collector 超时（{time.monotonic() - started:.1f} 秒）") from None
        failed = self._failures.take()
        if failed or not all(r is True for r in done):
            raise TransientError("export_failed", "导出到 Collector 失败")


class _Span:
    def __init__(self, span: trace.Span) -> None:
        self._span = span
        context = span.get_span_context()
        self._trace_id = format(context.trace_id, "032x")
        self._span_id = format(context.span_id, "016x")

    @property
    def trace_id(self) -> str:
        return self._trace_id

    @property
    def span_id(self) -> str:
        return self._span_id

    def set_attributes(self, attributes: Attributes) -> None:
        self._span.set_attributes(_otel_attributes(sanitize(attributes)))

    def add_event(self, name: str, attributes: Attributes | None = None) -> None:
        self._span.add_event(name, _otel_attributes(sanitize(attributes)))

    def record_error(self, error: BaseException) -> None:
        self._span.set_status(Status(StatusCode.ERROR))
        self._span.set_attribute("error.type", type(error).__name__)
        self._span.add_event("exception", {"exception.type": type(error).__name__})


def _otel_value(value: Any) -> Any:
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        items = list(value)
        if all(isinstance(v, str) for v in items) or all(isinstance(v, bool) for v in items):
            return items
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in items):
            return items
        return [str(v) for v in items]
    return str(value)


def _otel_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    return {k: _otel_value(v) for k, v in attributes.items() if v is not None}


class OtelTelemetry:
    def __init__(self, provider: OtelTelemetryProvider, scope: Scope) -> None:
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
        return _otel_attributes({**sanitize(attributes), **self._scope_attributes})

    @staticmethod
    def _require_name(name: str) -> None:
        if not isinstance(name, str) or not name:
            raise InvalidError("invalid_argument", "名称不能为空")

    def _parent(self) -> otel_context.Context | None:
        """嵌套在本进程的 span 里时用当前上下文；否则接上租户上下文里的 traceparent（没有时是新的 trace）。"""
        if trace.get_current_span().get_span_context().is_valid:
            return None
        if self._scope.traceparent:
            return TraceContextTextMapPropagator().extract({"traceparent": self._scope.traceparent}, context=otel_context.Context())
        return otel_context.Context()

    def start_span(self, name: str, *, attributes: Attributes | None = None) -> Any:
        self._require_name(name)
        return self._span(name, attributes)

    @asynccontextmanager
    async def _span(self, name: str, attributes: Attributes | None) -> AsyncIterator[_Span]:
        with self._provider._tracer.start_as_current_span(
            name, context=self._parent(), attributes=self._attributes(attributes), record_exception=False, set_status_on_exception=False
        ) as otel_span:
            span = _Span(otel_span)
            try:
                yield span
            except BaseException as error:
                span.record_error(error)
                raise
            finally:
                otel_span.set_attributes(_otel_attributes(self._scope_attributes))  # set_attributes 不能覆盖租户属性

    async def count(self, name: str, value: int = 1, *, attributes: Attributes | None = None) -> None:
        self._require_name(name)
        self._provider._instrument("counter", name).add(value, self._attributes(attributes))

    async def observe(self, name: str, value: float, *, attributes: Attributes | None = None) -> None:
        self._require_name(name)
        self._provider._instrument("histogram", name).record(value, self._attributes(attributes))

    async def log(self, level: LogLevel, message: str, *, attributes: Attributes | None = None) -> None:
        level = LogLevel(level)
        self._provider._logger.emit(
            timestamp=time.time_ns(),
            context=otel_context.get_current(),
            severity_number=_SEVERITY[level],
            severity_text=str(level),
            body=str(message),
            attributes=self._attributes(attributes),
        )

    async def flush(self) -> None:
        await self._provider._flush()


def telemetry_from_env(service_name: str, env: Mapping[str, str] | None = None, *, stream: TextIO | None = None) -> TelemetryProvider:
    """设了 OTEL_EXPORTER_OTLP_ENDPOINT（Collector 的 OTLP/HTTP 基地址）就用 OpenTelemetry 实现，否则用控制台实现（写 stream，默认标准错误）。
    服务版本与环境取 OTEL_SERVICE_VERSION、OTEL_DEPLOYMENT_ENVIRONMENT。"""
    env = os.environ if env is None else env
    endpoint = env.get(ENV_ENDPOINT, "").strip()
    if not endpoint:
        return ConsoleTelemetryProvider(stream=stream if stream is not None else sys.stderr)
    return OtelTelemetryProvider(
        endpoint,
        service_name=service_name,
        service_version=env.get(ENV_SERVICE_VERSION, "").strip(),
        environment=env.get(ENV_ENVIRONMENT, "").strip(),
    )
