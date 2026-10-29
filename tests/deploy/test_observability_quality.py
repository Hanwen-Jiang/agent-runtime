"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 9.2、9.3 节验收（部署一侧）。

细化见该计划第 11.9 节：
- 9.2：Grafana 加一个“模型与 Task 质量”面板（deploy/data/dashboards/quality.yaml）。调用链的统计查 Tempo（TraceQL 指标查询）：
  按模型的 chat 延迟 p50/p95 与错误率、按任务族的 Task 结局与花费、每个 Task 的 Round 数、Verdict 分布、评测运行（eval.run）。
  网关的 chat span 名字是“chat {模型}”，所以按 span.gen_ai.operation.name 过滤，不写 name = "chat"（那只选到 Worker 的 model 节点；
  原面板的“模型花费”一并改掉）。
- 9.3：指标后端是 VictoriaMetrics 单机版（2026-10-28 人定），装在 observability，保留 30 天。Collector 的 metrics 管道先把
  应用发来的 delta 指标转成累计（deltatocumulative），再经 prometheusremotewrite 写进去；地址由 Chart 注入
  （VM_REMOTE_WRITE_ENDPOINT）。Grafana 加 Prometheus 类型的数据源，趋势面板与告警（供应商不可用率、租户每小时花费）查它。
  指标名由 Telemetry 的名字换成 Prometheus 的写法：点换下划线，计数器加 _total，直方图有 _sum、_count、_bucket。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from deploy_tests import CLOUD_VALUES, COLLECTOR_CONFIG, REPO_ROOT, by_kind, containers, env_of, render

DATA = REPO_ROOT / "deploy" / "data"
VM_URL = "http://vm-victoria-metrics-single-server.observability.svc:8428"
QUALITY_TITLES = ("模型调用延迟", "模型调用错误率", "Task 结局（按任务族）", "Task 花费（按任务族）", "Round 数", "Verdict 分布", "评测")
TREND_TITLES = ("租户每小时花费", "供应商不可用率", "评测分数趋势")


def boards() -> dict[str, dict[str, Any]]:
    """deploy/data/dashboards 里每个 ConfigMap 带的面板，按 uid。"""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted((DATA / "dashboards").glob("*.yaml")):
        for cm in yaml.safe_load_all(path.read_text("utf-8")):
            assert cm["kind"] == "ConfigMap" and cm["metadata"]["namespace"] == "observability"
            assert cm["metadata"]["labels"].get("grafana_dashboard") == "1"
            for raw in cm["data"].values():
                board = json.loads(raw)
                out[board["uid"]] = board
    return out


def sources() -> str:
    """发 span 与指标的代码：Worker、Learning（评测）与 Model Gateway。"""
    roots = [
        (REPO_ROOT / "services" / "py-worker" / "src", "*.py"),
        (REPO_ROOT / "services" / "py-learning" / "src", "*.py"),
        (REPO_ROOT / "services" / "go-model-gateway", "*.go"),
    ]
    return "\n".join(p.read_text("utf-8") for root, pattern in roots for p in root.rglob(pattern) if not p.name.endswith("_test.go"))


def targets(board: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [(p, t) for p in board["panels"] for t in p.get("targets", [])]


def grafana() -> dict[str, Any]:
    return yaml.safe_load((DATA / "values" / "grafana.yaml").read_text("utf-8"))


def collector() -> dict[str, Any]:
    return yaml.safe_load(COLLECTOR_CONFIG.read_text("utf-8"))


# --- 9.2：模型与 Task 质量 ---


def test_the_quality_board_exists_with_its_panels() -> None:
    board = boards()["agent-runtime-quality"]
    assert board["title"] == "模型与 Task 质量"
    titles = " ".join(p["title"] for p in board["panels"])
    for wanted in (*QUALITY_TITLES, *TREND_TITLES):
        assert wanted in titles, f"缺面板“{wanted}”"


def test_trace_panels_query_spans_and_attributes_the_code_emits() -> None:
    code = sources()
    board = boards()["agent-runtime-quality"]
    tempo = [t for p, t in targets(board) if p["datasource"]["uid"] == "tempo"]
    assert tempo
    for t in tempo:
        for name in re.findall(r'(?<![.\w])name = "([^"]+)"', t["query"]):
            assert f'"{name}"' in code, f"面板查的 span {name} 代码里没有"
        for attr in re.findall(r"span\.([a-z_.]+)", t["query"]):
            if attr == "tenant_id":
                continue  # 绑定租户的 Telemetry 自动加上
            assert f'"{attr}"' in code, f"面板用的属性 {attr} 代码里没有"


def test_chat_spans_are_selected_by_operation_not_by_name() -> None:
    """Model Gateway 的 span 叫“chat {模型}”，name = "chat" 只选到 Worker 的 model 节点，漏了 Planner、Reviewer 等直接经网关的调用
    （原面板“模型花费”的毛病，一并改掉）。"""
    for uid, board in boards().items():
        for _, t in targets(board):
            assert not re.search(r'(?<![.\w])name = "chat"', t.get("query", "")), f"{uid} 的查询按名字选 chat span"
    queries = " ".join(t["query"] for _, t in targets(boards()["agent-runtime-quality"]) if t.get("query"))
    assert 'span.gen_ai.operation.name = "chat"' in queries


def prometheus_name(telemetry_name: str) -> str:
    return telemetry_name.replace(".", "_")


def test_trend_panels_query_metrics_the_code_emits() -> None:
    """VictoriaMetrics 的查询用到的指标都由代码经 Telemetry 发出（名字换写法之后对得上）。"""
    code = sources()
    emitted = {prometheus_name(n) for n in re.findall(r'"((?:runtime|gen_ai)\.[a-z_.]+)"', code)}
    board = boards()["agent-runtime-quality"]
    trends = [(p, t) for p, t in targets(board) if p["datasource"]["uid"] == "victoriametrics"]
    assert {p["title"] for p, _ in trends} >= set(TREND_TITLES)
    for _, t in trends:
        names = re.findall(r"\b((?:runtime|gen_ai)_[a-z_]+?)(?:_total|_sum|_count|_bucket)?\b", t["expr"])
        assert names, t["expr"]
        for name in names:
            assert name in emitted, f"面板查的指标 {name} 代码里没有发"


# --- 9.3：VictoriaMetrics ---


def test_victoriametrics_keeps_thirty_days() -> None:
    values = yaml.safe_load((DATA / "values" / "victoria-metrics.yaml").read_text("utf-8"))
    server = values["server"]
    assert str(server["retentionPeriod"]) == "30d"
    assert server["persistentVolume"]["enabled"] is True
    assert "resources" in server


def test_victoriametrics_is_installed_in_observability_at_a_pinned_version() -> None:
    install = (DATA / "install.sh").read_text("utf-8")
    assert "helm repo add vm https://victoriametrics.github.io/helm-charts" in install
    assert re.search(r'helm upgrade --install vm vm/victoria-metrics-single --version "\$VM_CHART_VERSION" -n observability -f "\$HERE/values/victoria-metrics.yaml"', install)
    versions = (REPO_ROOT / "deploy" / "cluster" / "versions.conf").read_text("utf-8")
    assert re.search(r"^VM_CHART_VERSION=\d+\.\d+\.\d+$", versions, re.MULTILINE)


def test_grafana_reads_victoriametrics_as_prometheus() -> None:
    sources_ = grafana()["datasources"]["datasources.yaml"]["datasources"]
    vm = next(s for s in sources_ if s["uid"] == "victoriametrics")
    assert vm["type"] == "prometheus" and vm["url"] == VM_URL and vm["access"] == "proxy"
    assert next(s for s in sources_ if s["uid"] == "tempo")["isDefault"] is True, "Tempo 仍是默认数据源"


def test_grafana_alerts_on_provider_outages_and_tenant_spend() -> None:
    rules = [r for g in grafana()["alerting"]["rules.yaml"]["groups"] for r in g["rules"]]
    titles = {r["title"] for r in rules}
    assert {"模型供应商不可用率", "租户每小时花费"} <= titles
    for rule in rules:
        queries = [d for d in rule["data"] if d.get("datasourceUid") == "victoriametrics"]
        assert queries, f"{rule['title']} 查 VictoriaMetrics"
    by_title = {r["title"]: json.dumps(r, ensure_ascii=False) for r in rules}
    assert "runtime_model_calls_total" in by_title["模型供应商不可用率"]
    assert "runtime_model_cost_usd" in by_title["租户每小时花费"]


def test_the_collector_writes_metrics_to_victoriametrics() -> None:
    config = collector()
    exporter = config["exporters"]["prometheusremotewrite/victoriametrics"]
    assert exporter["endpoint"] == "${env:VM_REMOTE_WRITE_ENDPOINT}"
    assert "deltatocumulative" in config["processors"], "应用按 delta 时间性发指标，remote write 要累计值"
    metrics = config["service"]["pipelines"]["metrics"]
    assert metrics["processors"] == ["memory_limiter", "attributes/redact", "deltatocumulative", "batch"]
    assert metrics["exporters"] == ["prometheusremotewrite/victoriametrics"]
    assert config["service"]["pipelines"]["logs"]["exporters"] == ["debug"], "日志的后端仍没选型"


def test_the_chart_passes_the_remote_write_endpoint_of_the_data_layer() -> None:
    cloud = render(CLOUD_VALUES)
    envs = [env_of(c) for m, c in containers(by_kind(cloud, "Deployment")) if c["name"] == "otel-collector"]
    assert envs and envs[0]["VM_REMOTE_WRITE_ENDPOINT"]["value"] == f"{VM_URL}/api/v1/write"
    defaults = yaml.safe_load((REPO_ROOT / "deploy" / "helm" / "agent-runtime" / "values.yaml").read_text("utf-8"))
    assert defaults["external"]["victoriametrics"]["remoteWrite"], "默认 values 也要给一个地址（开发用的随便填）"


@pytest.mark.parametrize("path", sorted(Path(DATA / "dashboards").glob("*.yaml")))
def test_every_dashboard_file_is_a_valid_configmap(path: Path) -> None:
    for cm in yaml.safe_load_all(path.read_text("utf-8")):
        for raw in cm["data"].values():
            json.loads(raw)
