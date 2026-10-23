"""T-15：OpenTelemetry Collector 的配置（设计书 4.13、L.14；细化见 docs/plan/deploy-telemetry.md 第 2 节第 3 条）。

一路收，三路发：全部 span → Tempo（otlp）；语义 span → Langfuse（otlphttp，/api/public/otel）与 Redpanda（kafka，otlp_json，
主题 telemetry.spans，供 Learning Pipeline 消费）。每条管道先 memory_limiter、后 batch，都经过 attributes/redact。
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any

import pytest
import yaml

from agentruntime_core.telemetry_rules import GENAI_USAGE_COUNT_KEYS, SENSITIVE_KEY_PARTS

from deploy_tests import COLLECTOR_CONFIG, require_tool

SEMANTIC_PIPELINES = ("traces/langfuse", "traces/learning")


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    return yaml.safe_load(COLLECTOR_CONFIG.read_text(encoding="utf-8"))


def pipelines(config: dict[str, Any]) -> dict[str, Any]:
    return config["service"]["pipelines"]


def test_receives_otlp_on_both_protocols(config: dict[str, Any]) -> None:
    protocols = config["receivers"]["otlp"]["protocols"]
    assert protocols["grpc"]["endpoint"].endswith(":4317")
    assert protocols["http"]["endpoint"].endswith(":4318")


def test_three_trace_destinations(config: dict[str, Any]) -> None:
    p = pipelines(config)
    assert p["traces/tempo"]["exporters"] == ["otlp/tempo"]
    assert p["traces/langfuse"]["exporters"] == ["otlphttp/langfuse"]
    assert p["traces/learning"]["exporters"] == ["kafka"]
    for name in ("traces/tempo", *SEMANTIC_PIPELINES):
        assert p[name]["receivers"] == ["otlp"]


def test_tempo_takes_otlp_grpc(config: dict[str, Any]) -> None:
    tempo = config["exporters"]["otlp/tempo"]
    assert tempo["endpoint"] == "${env:TEMPO_ENDPOINT}"


def test_langfuse_takes_otlp_http_with_basic_auth_from_the_environment(config: dict[str, Any]) -> None:
    """Langfuse 只收 OTLP over HTTP（L.14）；认证取自环境变量，不写死在配置里。"""
    lf = config["exporters"]["otlphttp/langfuse"]
    assert lf["traces_endpoint"] == "${env:LANGFUSE_ENDPOINT}/api/public/otel/v1/traces"
    assert lf["headers"]["Authorization"] == "Basic ${env:LANGFUSE_AUTH}"


def test_learning_gets_otlp_json_on_the_spans_topic(config: dict[str, Any]) -> None:
    kafka = config["exporters"]["kafka"]
    assert kafka["brokers"] == ["${env:REDPANDA_BROKERS}"]
    traces = kafka.get("traces", kafka)
    assert traces["topic"] == "telemetry.spans"
    assert traces["encoding"] == "otlp_json"


def test_every_pipeline_limits_memory_first_batches_last_and_redacts(config: dict[str, Any]) -> None:
    for name, p in pipelines(config).items():
        procs = p["processors"]
        assert procs[0] == "memory_limiter", f"{name}：memory_limiter 要在最前"
        assert procs[-1] == "batch", f"{name}：batch 要在最后"
        assert "attributes/redact" in procs, f"{name}：要经过脱敏"
        for proc in procs:
            assert proc in config["processors"], f"{name} 引用了没定义的处理器 {proc}"
        for exp in p["exporters"]:
            assert exp in config["exporters"], f"{name} 引用了没定义的导出器 {exp}"


def test_semantic_pipelines_drop_infrastructure_spans(config: dict[str, Any]) -> None:
    for name in SEMANTIC_PIPELINES:
        assert "filter/semantic" in pipelines(config)[name]["processors"]
    assert "filter/semantic" not in pipelines(config)["traces/tempo"]["processors"], "Tempo 收全部 span（第 4 节第 5 条）"
    rules = yaml.safe_dump(config["processors"]["filter/semantic"])
    for marker in ("gen_ai.operation.name", "runtime", "invoke_"):
        assert marker in rules, f"filter/semantic 要按 {marker} 留下语义 span"


def test_metrics_and_logs_go_to_debug_for_now(config: dict[str, Any]) -> None:
    p = pipelines(config)
    assert p["metrics"]["exporters"] == ["debug"] and p["logs"]["exporters"] == ["debug"]
    assert config["exporters"]["debug"].get("verbosity", "basic") == "basic", "debug 只打摘要，不打属性"


def redact_patterns(config: dict[str, Any]) -> list[re.Pattern[str]]:
    actions = config["processors"]["attributes/redact"]["actions"]
    assert all(a["action"] == "delete" for a in actions)
    return [re.compile(a["pattern"]) for a in actions]


@pytest.mark.parametrize("part", sorted(set(SENSITIVE_KEY_PARTS) - {"token"}))
def test_collector_redaction_matches_the_application_rules(config: dict[str, Any], part: str) -> None:
    """第二道脱敏与应用端同一份列表（token 除外：RE2 写不出“含 token 但不是 GenAI 计数”，只靠应用端）。"""
    patterns = redact_patterns(config)
    for key in (part, part.upper(), f"x_{part}_id", f"db.{part}"):
        assert any(p.search(key) for p in patterns), f"Collector 删不掉 {key}"


@pytest.mark.parametrize("key", [*GENAI_USAGE_COUNT_KEYS, "tenant_id", "gen_ai.operation.name", "runtime.cost_usd", "temporal.workflow_id"])
def test_collector_keeps_ordinary_attributes(config: dict[str, Any], key: str) -> None:
    assert not any(p.search(key) for p in redact_patterns(config)), f"Collector 不该删 {key}"


def test_collector_validates_its_own_config() -> None:
    """有 otelcol-contrib 时用它自己的 validate 校验（缺时按 AR_ALLOW_MISSING_TOOLS 跳过）。"""
    otelcol = require_tool("otelcol-contrib")
    env_defaults = {"TEMPO_ENDPOINT": "tempo:4317", "LANGFUSE_ENDPOINT": "http://langfuse:3000", "LANGFUSE_AUTH": "eDp5", "REDPANDA_BROKERS": "redpanda:9092"}
    proc = subprocess.run(
        [otelcol, "validate", f"--config={COLLECTOR_CONFIG}"],
        env={**os.environ, **env_defaults},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
