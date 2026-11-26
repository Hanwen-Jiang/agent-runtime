"""Lightweight Prometheus text-format metrics exporter for py-memory (no external deps)."""

from __future__ import annotations

import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_DURATION_BUCKETS: tuple[float, ...] = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class MemoryMetricsRegistry:
    """Thread-safe in-process registry for py-memory retrieval & cache metrics."""

    def __init__(self, buckets: tuple[float, ...] = DEFAULT_DURATION_BUCKETS) -> None:
        self._lock = threading.Lock()
        self.buckets = tuple(sorted(buckets))
        self._search_requests: dict[tuple[str, str], int] = {}
        self._search_duration_buckets: dict[str, list[int]] = {}
        self._search_duration_sum: dict[str, float] = {}
        self._search_duration_count: dict[str, int] = {}
        self._rerank_calls: dict[str, int] = {}
        self._rerank_skipped: dict[str, int] = {}
        self._cache_hits = 0
        self._cache_misses = 0

    def inc_search(self, scope: str, status: str = "ok") -> None:
        key = (str(scope), str(status))
        with self._lock:
            self._search_requests[key] = self._search_requests.get(key, 0) + 1

    def observe_search_duration(self, scope: str, seconds: float) -> None:
        s = str(scope)
        val = max(0.0, float(seconds))
        with self._lock:
            counts = self._search_duration_buckets.setdefault(s, [0] * (len(self.buckets) + 1))
            for idx, bound in enumerate(self.buckets):
                if val <= bound:
                    counts[idx] += 1
            counts[-1] += 1  # +Inf
            self._search_duration_sum[s] = self._search_duration_sum.get(s, 0.0) + val
            self._search_duration_count[s] = self._search_duration_count.get(s, 0) + 1

    def inc_rerank_call(self, backend: str = "configured") -> None:
        key = str(backend)
        with self._lock:
            self._rerank_calls[key] = self._rerank_calls.get(key, 0) + 1

    def inc_rerank_skip(self, reason: str) -> None:
        key = str(reason)
        with self._lock:
            self._rerank_skipped[key] = self._rerank_skipped.get(key, 0) + 1

    def inc_cache_hit(self) -> None:
        with self._lock:
            self._cache_hits += 1

    def inc_cache_miss(self) -> None:
        with self._lock:
            self._cache_misses += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "search_requests": dict(self._search_requests),
                "rerank_calls": dict(self._rerank_calls),
                "rerank_skipped": dict(self._rerank_skipped),
                "cache_hits": self._cache_hits,
                "cache_misses": self._cache_misses,
            }

    def render_prometheus(self) -> str:
        with self._lock:
            lines: list[str] = [
                "# HELP ar_memory_search_requests_total Total search requests handled by py-memory.",
                "# TYPE ar_memory_search_requests_total counter",
            ]
            for (scope, status), val in sorted(self._search_requests.items()):
                lines.append(
                    f'ar_memory_search_requests_total{{scope="{_escape_label(scope)}",status="{_escape_label(status)}"}} {val}'
                )

            lines.extend([
                "# HELP ar_memory_search_duration_seconds Search latency distribution in seconds.",
                "# TYPE ar_memory_search_duration_seconds histogram",
            ])
            for scope, counts in sorted(self._search_duration_buckets.items()):
                sc = _escape_label(scope)
                for bound, c in zip(self.buckets, counts[:-1], strict=True):
                    lines.append(f'ar_memory_search_duration_seconds_bucket{{scope="{sc}",le="{bound}"}} {c}')
                lines.append(f'ar_memory_search_duration_seconds_bucket{{scope="{sc}",le="+Inf"}} {counts[-1]}')
                lines.append(f'ar_memory_search_duration_seconds_sum{{scope="{sc}"}} {self._search_duration_sum.get(scope, 0.0):.6f}')
                lines.append(f'ar_memory_search_duration_seconds_count{{scope="{sc}"}} {self._search_duration_count.get(scope, 0)}')

            lines.extend([
                "# HELP ar_memory_rerank_calls_total Cross-encoder / gateway reranker invocations.",
                "# TYPE ar_memory_rerank_calls_total counter",
            ])
            for backend, val in sorted(self._rerank_calls.items()):
                lines.append(f'ar_memory_rerank_calls_total{{backend="{_escape_label(backend)}"}} {val}')

            lines.extend([
                "# HELP ar_memory_rerank_skipped_total Reranker invocations skipped by gating policy.",
                "# TYPE ar_memory_rerank_skipped_total counter",
            ])
            for reason, val in sorted(self._rerank_skipped.items()):
                lines.append(f'ar_memory_rerank_skipped_total{{reason="{_escape_label(reason)}"}} {val}')

            lines.extend([
                "# HELP ar_memory_embedding_cache_hits_total Query embedding cache hits.",
                "# TYPE ar_memory_embedding_cache_hits_total counter",
                f"ar_memory_embedding_cache_hits_total {self._cache_hits}",
                "# HELP ar_memory_embedding_cache_misses_total Query embedding cache misses.",
                "# TYPE ar_memory_embedding_cache_misses_total counter",
                f"ar_memory_embedding_cache_misses_total {self._cache_misses}",
            ])
        return "\n".join(lines) + "\n"


_DEFAULT_REGISTRY = MemoryMetricsRegistry()


def default_metrics() -> MemoryMetricsRegistry:
    return _DEFAULT_REGISTRY


def start_metrics_server(address: str, registry: MemoryMetricsRegistry | None = None) -> ThreadingHTTPServer:
    """Bind a daemon HTTP server serving /metrics in Prometheus text exposition format."""
    reg = registry or _DEFAULT_REGISTRY
    host, _, port_str = address.rpartition(":")
    host = host or "0.0.0.0"
    port = int(port_str)

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] == "/metrics":
                body = reg.render_prometheus().encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/healthz":
                body = b"ok\n"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer((host, port), _Handler)
    thread = threading.Thread(target=server.serve_forever, name="py-memory-metrics", daemon=True)
    thread.start()
    return server
