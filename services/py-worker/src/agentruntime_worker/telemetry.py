"""Worker 进程的 Telemetry（设计书 4.13；T-15）。

设了 OTEL_EXPORTER_OTLP_ENDPOINT 时用 OpenTelemetry 实现，整个进程共用一个（导出器与缓冲只建一份）；
没设时与之前一样，每处组装各用一个写标准错误的控制台实现（开发与测试）。
"""

from __future__ import annotations

import os
import sys
import threading

from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.providers.telemetry import TelemetryProvider

SERVICE_NAME = "worker"
_lock = threading.Lock()
_shared: TelemetryProvider | None = None


def worker_telemetry() -> TelemetryProvider:
    global _shared
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
        return ConsoleTelemetryProvider(stream=sys.stderr)
    with _lock:
        if _shared is None:
            from agentruntime_core.adapters.otel import telemetry_from_env

            _shared = telemetry_from_env(SERVICE_NAME)
        return _shared
