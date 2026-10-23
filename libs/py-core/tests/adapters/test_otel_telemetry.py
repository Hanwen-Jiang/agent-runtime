"""T-15：OpenTelemetry 实现的 Telemetry（设计书 4.13；细化见 docs/plan/deploy-telemetry.md）。

记录经真的 OTLP/HTTP 导出器发到假 Collector（tests/adapters/otel_collector.py），解码之后跑与控制台实现相同的一致性测试；
另外检查只有这个实现才有的部分：资源属性、导出失败与 flush 的超时、按环境变量选实现。
"""

from __future__ import annotations

import asyncio
import io
import time
from collections.abc import Iterator
from datetime import timedelta

import pytest

pytest.importorskip("opentelemetry.sdk", reason="要装 agentruntime-core 的 otel extra")

from agentruntime_core.adapters.otel import OtelTelemetryProvider, telemetry_from_env  # noqa: E402
from agentruntime_core.conformance.common import tenant_a  # noqa: E402
from agentruntime_core.conformance.telemetry import Exported, TelemetryConformance  # noqa: E402
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider  # noqa: E402
from agentruntime_core.errors import ErrorCategory, ProviderError  # noqa: E402
from agentruntime_core.providers.telemetry import LogLevel  # noqa: E402

from .otel_collector import FakeCollector, running_collector  # noqa: E402

EXPORT_TIMEOUT = timedelta(seconds=1)


class Harness:
    def __init__(self, collector: FakeCollector) -> None:
        self.collector = collector
        self.provider = OtelTelemetryProvider(
            collector.endpoint, service_name="conformance", service_version="0.0.0", environment="test", export_timeout=EXPORT_TIMEOUT
        )

    async def advance(self, delta: timedelta) -> None:
        await asyncio.sleep(delta.total_seconds())

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        assert operation == "export" and category is ErrorCategory.TRANSIENT
        self.collector.fail_all()

    def exported(self) -> Exported:
        return self.collector.exported()


@pytest.fixture
def collector() -> Iterator[FakeCollector]:
    with running_collector() as c:
        yield c


@pytest.fixture
def harness(collector: FakeCollector) -> Iterator[Harness]:
    # 同步 fixture：BindingRules 的测试是同步的。导出器在 SDK 的后台线程里，关闭不依赖测试的事件循环。
    h = Harness(collector)
    yield h
    asyncio.run(h.provider.aclose())


class TestOtelTelemetry(TelemetryConformance):
    pass


# --- 只有 OpenTelemetry 实现才有的部分 ---


async def test_records_travel_as_otlp_http_protobuf(harness: Harness) -> None:
    telemetry = harness.provider.bind(tenant_a())
    async with telemetry.start_span("runtime.round"):
        await telemetry.count("rounds")
        await telemetry.log(LogLevel.INFO, "round started")
    await telemetry.flush()
    paths = {p for p, _ in harness.collector.requests}
    assert {"/v1/traces", "/v1/metrics", "/v1/logs"} <= paths
    for _, headers in harness.collector.requests:
        assert headers["content-type"] == "application/x-protobuf"


async def test_resource_names_the_service_and_never_a_tenant(harness: Harness) -> None:
    telemetry = harness.provider.bind(tenant_a())
    async with telemetry.start_span("node"):
        await telemetry.count("calls")
    await telemetry.flush()
    assert harness.collector.resources, "每批记录都带资源属性"
    for resource in harness.collector.resources:
        assert resource["service.name"] == "conformance"
        assert resource["service.version"] == "0.0.0"
        assert resource["deployment.environment"] == "test"
        assert not any(k in resource for k in ("tenant_id", "user_id", "session_id")), "一个进程服务多个租户，租户只在记录上"


async def test_unreachable_collector_only_shows_up_in_flush(harness: Harness) -> None:
    harness.collector.fail_all()
    telemetry = harness.provider.bind(tenant_a())
    async with telemetry.start_span("node"):  # 业务调用照常返回
        await telemetry.count("calls")
        await telemetry.observe("latency_ms", 3.0)
        await telemetry.log(LogLevel.INFO, "still fine")
    with pytest.raises(ProviderError) as err:
        await telemetry.flush()
    assert err.value.category is ErrorCategory.TRANSIENT


async def test_collector_recovers_after_a_failed_flush(harness: Harness) -> None:
    harness.collector.fail_all()
    telemetry = harness.provider.bind(tenant_a())
    async with telemetry.start_span("lost"):
        pass
    with pytest.raises(ProviderError):
        await telemetry.flush()
    harness.collector.recover()
    async with telemetry.start_span("delivered"):
        pass
    await telemetry.flush()
    assert "delivered" in {s.name for s in harness.collector.exported().spans}


async def test_flush_gives_up_within_the_export_timeout(harness: Harness) -> None:
    """Collector 卡住时 flush 不能把 activity 卡住：在导出超时（加一点余量）内以 transient 返回。"""
    harness.collector.hang(10)
    telemetry = harness.provider.bind(tenant_a())
    async with telemetry.start_span("node"):
        pass
    started = time.monotonic()
    with pytest.raises(ProviderError) as err:
        await telemetry.flush()
    assert err.value.category is ErrorCategory.TRANSIENT
    assert time.monotonic() - started < EXPORT_TIMEOUT.total_seconds() * 3 + 1


async def test_start_span_does_not_wait_for_the_collector(harness: Harness) -> None:
    harness.collector.hang(5)
    telemetry = harness.provider.bind(tenant_a())
    started = time.monotonic()
    for _ in range(20):
        async with telemetry.start_span("node"):
            await telemetry.count("calls")
    assert time.monotonic() - started < 1, "打点只进缓冲，不等导出"


# --- 按环境变量选实现 ---


def test_endpoint_variable_selects_opentelemetry() -> None:
    provider = telemetry_from_env("worker", env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"})
    assert isinstance(provider, OtelTelemetryProvider)
    assert provider.service_name == "worker"


def test_without_the_endpoint_the_console_implementation_stays() -> None:
    stream = io.StringIO()
    provider = telemetry_from_env("worker", env={}, stream=stream)
    assert isinstance(provider, ConsoleTelemetryProvider)


def test_blank_endpoint_counts_as_unset() -> None:
    assert isinstance(telemetry_from_env("worker", env={"OTEL_EXPORTER_OTLP_ENDPOINT": "  "}), ConsoleTelemetryProvider)


def test_service_version_and_environment_come_from_standard_variables() -> None:
    provider = telemetry_from_env(
        "memory",
        env={
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
            "OTEL_SERVICE_VERSION": "1.2.3",
            "OTEL_DEPLOYMENT_ENVIRONMENT": "staging",
        },
    )
    assert isinstance(provider, OtelTelemetryProvider)
    assert (provider.service_version, provider.environment) == ("1.2.3", "staging")
