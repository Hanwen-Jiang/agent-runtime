"""F4（docs/plan/open-signup.md 第 2 节第 6 条）：开放注册后的花费兜底面板与告警。

已经有的（不在这里重复）：“租户每小时花费”面板与告警（单个租户一小时超过 5 美元），见 test_observability_quality.py。
这里要的：
- “模型与 Task 质量”看板上加“全平台 24 小时花费”（stat）与“租户 24 小时花费（前 20）”（table），都按 Model Gateway 计的费
  （runtime_model_cost_usd，按 tenant_id）；
- 告警“全平台 24 小时花费”：全平台最近 24 小时的模型花费超过阈值（默认 50 美元，Grafana 的 values 里可改）时告警。
通知渠道（邮件或 webhook）要人定，定了再加 contact point 的测试。实现之前（看板上还没有 24 小时的面板）这些测试跳过。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

DATA = Path(__file__).resolve().parents[2] / "deploy" / "data"
PLATFORM_EXPR = "sum(increase(runtime_model_cost_usd_sum[24h]))"
TENANTS_EXPR = "topk(20, sum by (tenant_id) (increase(runtime_model_cost_usd_sum[24h])))"


def quality_board() -> dict[str, Any]:
    for cm in yaml.safe_load_all((DATA / "dashboards" / "quality.yaml").read_text("utf-8")):
        for raw in cm["data"].values():
            return json.loads(raw)
    raise AssertionError("quality.yaml 里没有看板")


def panels() -> dict[str, dict[str, Any]]:
    board = quality_board()
    found = {p["title"]: p for p in board["panels"]}
    if not any("24 小时" in t for t in found):
        pytest.skip("24 小时的花费面板还没加：先审验收测试（docs/plan/open-signup.md 第 2 节第 6 条）")
    return found


def rules() -> dict[str, dict[str, Any]]:
    values = yaml.safe_load((DATA / "values" / "grafana.yaml").read_text("utf-8"))
    return {r["title"]: r for g in values["alerting"]["rules.yaml"]["groups"] for r in g["rules"]}


def test_the_board_shows_platform_and_per_tenant_spend_for_the_last_day() -> None:
    found = panels()
    platform, tenants = found["全平台 24 小时花费"], found["租户 24 小时花费（前 20）"]
    assert platform["type"] == "stat" and [t["expr"] for t in platform["targets"]] == [PLATFORM_EXPR]
    assert tenants["type"] == "table" and [t["expr"] for t in tenants["targets"]] == [TENANTS_EXPR]
    for panel in (platform, tenants):
        assert panel["datasource"]["uid"] == "victoriametrics"
        assert panel["fieldConfig"]["defaults"]["unit"] == "currencyUSD"


def test_platform_daily_spend_alerts_above_the_threshold() -> None:
    panels()
    rule = rules()["全平台 24 小时花费"]
    assert rule["uid"] == "ar-platform-daily-spend" and rule["labels"]["severity"] == "critical"
    [query] = [d for d in rule["data"] if d.get("datasourceUid") == "victoriametrics"]
    assert query["model"]["expr"] == PLATFORM_EXPR and query["relativeTimeRange"]["from"] == 86400
    [threshold] = [d for d in rule["data"] if d.get("model", {}).get("type") == "threshold"]
    assert threshold["model"]["conditions"][0]["evaluator"] == {"type": "gt", "params": [50]}
    assert "50 美元" in rule["annotations"]["summary"]
    assert rule["noDataState"] == "OK", "没有调用时不告警"


def test_the_existing_hourly_tenant_alert_stays() -> None:
    panels()
    assert "租户每小时花费" in rules(), "原来的单租户每小时告警留着"
