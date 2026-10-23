"""测试用的假 OpenTelemetry Collector：在 127.0.0.1 上收 OTLP/HTTP（protobuf）的 /v1/traces、/v1/metrics、/v1/logs，
解码成一致性测试的 Exported。

- fail_all()：之后的请求一律 503（可重试的导出失败），直到 recover()。
- hang(seconds)：之后的请求先等这么久再回 200，用来检查 flush 的超时上限。
- requests：收到的 (路径, 请求头) 列表；resources：每批记录的资源属性。
"""

from __future__ import annotations

import gzip
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Status

from agentruntime_core.conformance.telemetry import Exported, ExportedLog, ExportedMetric, ExportedSpan


def any_value(value: AnyValue) -> Any:
    kind = value.WhichOneof("value")
    if kind == "array_value":
        return [any_value(v) for v in value.array_value.values]
    if kind == "kvlist_value":
        return attributes(value.kvlist_value.values)
    if kind == "bytes_value":
        return value.bytes_value.hex()
    return getattr(value, kind) if kind else None


def attributes(items: Any) -> dict[str, Any]:
    return {kv.key: any_value(kv.value) for kv in items if isinstance(kv, KeyValue)}


class FakeCollector:
    def __init__(self) -> None:
        self.spans: list[ExportedSpan] = []
        self.metrics: list[ExportedMetric] = []
        self.logs: list[ExportedLog] = []
        self.resources: list[dict[str, Any]] = []
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.raw: list[bytes] = []
        self._failing = False
        self._delay = 0.0
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeCollector:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def fail_all(self) -> None:
        self._failing = True

    def recover(self) -> None:
        self._failing = False

    def hang(self, seconds: float) -> None:
        self._delay = seconds

    def exported(self) -> Exported:
        with self._lock:
            # 原始请求体也放进去：明文不能出现在线上传输的任何字节里。
            wire = ExportedLog(level="wire", message=repr(b"".join(self.raw)))
            return Exported(spans=tuple(self.spans), metrics=tuple(self.metrics), logs=(*self.logs, wire))

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # 不往测试输出里打访问日志
                return None

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                collector.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                if collector._delay:
                    time.sleep(collector._delay)
                if collector._failing:
                    self.send_response(503)
                    self.end_headers()
                    return
                with collector._lock:
                    collector.raw.append(body)
                    collector._decode(self.path, body)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.end_headers()

        return Handler

    def _decode(self, path: str, body: bytes) -> None:
        if path == "/v1/traces":
            traces = ExportTraceServiceRequest.FromString(body)
            for rs in traces.resource_spans:
                self.resources.append(attributes(rs.resource.attributes))
                for ss in rs.scope_spans:
                    for s in ss.spans:
                        events = []
                        for e in s.events:
                            attrs = attributes(e.attributes)
                            events.append(e.name if not attrs else f"{e.name} {json.dumps(attrs, ensure_ascii=False, default=str)}")
                        self.spans.append(
                            ExportedSpan(
                                name=s.name,
                                trace_id=s.trace_id.hex(),
                                span_id=s.span_id.hex(),
                                parent_span_id=s.parent_span_id.hex(),
                                attributes=attributes(s.attributes),
                                status="error" if s.status.code == Status.STATUS_CODE_ERROR else "ok",
                                events=tuple(events),
                            )
                        )
        elif path == "/v1/metrics":
            metrics = ExportMetricsServiceRequest.FromString(body)
            for rm in metrics.resource_metrics:
                self.resources.append(attributes(rm.resource.attributes))
                for sm in rm.scope_metrics:
                    for m in sm.metrics:
                        kind = m.WhichOneof("data")
                        if kind == "sum":
                            for p in m.sum.data_points:
                                value = p.as_int if p.WhichOneof("value") == "as_int" else p.as_double
                                self.metrics.append(ExportedMetric(name=m.name, kind="counter", value=value, attributes=attributes(p.attributes)))
                        elif kind == "histogram":
                            for p in m.histogram.data_points:
                                self.metrics.append(ExportedMetric(name=m.name, kind="histogram", value=p.sum, attributes=attributes(p.attributes)))
        elif path == "/v1/logs":
            logs = ExportLogsServiceRequest.FromString(body)
            for rl in logs.resource_logs:
                self.resources.append(attributes(rl.resource.attributes))
                for sl in rl.scope_logs:
                    for r in sl.log_records:
                        self.logs.append(ExportedLog(level=r.severity_text.lower(), message=str(any_value(r.body)), attributes=attributes(r.attributes)))


@contextmanager
def running_collector() -> Iterator[FakeCollector]:
    collector = FakeCollector().start()
    try:
        yield collector
    finally:
        collector.stop()
