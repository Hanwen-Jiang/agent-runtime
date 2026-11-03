"""N-20 的缺口（docs/plan/n19-n20-implementation-2026-11-03.md“没做的”，docs/plan/lane-merge-2026-11-03.md 第 3 节）：
Grafana 上首事件延迟按段的分位数。

Worker 收尾时记直方图 runtime.task.first_event_ms（属性 segment：queue、planning、sandbox_cold_start、first_token、first_event），
经 Collector 写进 VictoriaMetrics 是 runtime_task_first_event_ms_bucket。“模型与 Task 质量”看板加一个面板：p50 与 p95，按段，
最近一小时的窗口，单位毫秒。实现之前（看板上还没有这个面板）跳过。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TITLE = "首事件延迟（毫秒，p50 与 p95，按段）"


def panel() -> dict[str, Any]:
    for cm in yaml.safe_load_all((ROOT / "deploy" / "data" / "dashboards" / "quality.yaml").read_text("utf-8")):
        for raw in cm["data"].values():
            for p in json.loads(raw)["panels"]:
                if p["title"] == TITLE:
                    return p
    pytest.skip("首事件延迟的面板还没加：先审验收测试")


def test_the_panel_shows_p50_and_p95_by_segment() -> None:
    p = panel()
    assert p["type"] == "timeseries" and p["datasource"]["uid"] == "victoriametrics"
    assert p["fieldConfig"]["defaults"]["unit"] == "ms"
    exprs = sorted(t["expr"] for t in p["targets"])
    assert exprs == sorted(
        f"histogram_quantile({q}, sum by (le, segment) (increase(runtime_task_first_event_ms_bucket[1h])))" for q in ("0.5", "0.95")
    )
    assert all("{{segment}}" in t["legendFormat"] for t in p["targets"])


def test_the_metric_and_segments_are_what_the_worker_emits() -> None:
    panel()
    wrap_up = (ROOT / "services" / "py-worker" / "src" / "agentruntime_worker" / "reviewer" / "wrap_up.py").read_text("utf-8")
    latency = (ROOT / "services" / "py-worker" / "src" / "agentruntime_worker" / "workflows" / "latency.py").read_text("utf-8")
    assert '"runtime.task.first_event_ms"' in wrap_up and '{"segment": name}' in wrap_up
    assert 'SEGMENTS = ("queue", "planning", "sandbox_cold_start", "first_token", "first_event")' in latency
