"""G-05 验收（coding-agent 仓库 docs/plan/github-app.md 第 4 节 G-05 第 1 条）：coding-web 的 GitHub App 收件箱告警。

coding-web 经 libs/go-core 的 Telemetry 发 coding.github.*（计数器在 VictoriaMetrics 里加 _total；积压是 observe，
有 _sum 与 _count），经本仓库的 Collector 写进 VictoriaMetrics。Grafana 上死信、积压、签名失败突增要告警。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

DATA = Path(__file__).resolve().parents[2] / "deploy" / "data"

EXPECTED = {
    "coding-github-dead-letters": (
        "sum(increase(coding_github_processed_total{result=\"dead\"}[1h]))",
        {"type": "gt", "params": [0]},
    ),
    "coding-github-backlog": (
        "max(increase(coding_github_backlog_sum[15m]) / increase(coding_github_backlog_count[15m]))",
        {"type": "gt", "params": [50]},
    ),
    "coding-github-signature-failures": (
        "sum(increase(coding_github_deliveries_total{result=\"signature_failed\"}[15m]))",
        {"type": "gt", "params": [10]},
    ),
}


def rules() -> dict[str, dict[str, Any]]:
    values = yaml.safe_load((DATA / "values" / "grafana.yaml").read_text("utf-8"))
    return {r["uid"]: r for g in values["alerting"]["rules.yaml"]["groups"] for r in g["rules"]}


@pytest.mark.parametrize("uid", sorted(EXPECTED))
def test_coding_web_alerts_on_the_github_inbox(uid: str) -> None:
    expr, evaluator = EXPECTED[uid]
    rule = rules()[uid]
    [query] = [d for d in rule["data"] if d.get("datasourceUid") == "victoriametrics"]
    assert query["model"]["expr"] == expr
    [threshold] = [d for d in rule["data"] if d.get("model", {}).get("type") == "threshold"]
    assert threshold["model"]["conditions"][0]["evaluator"] == evaluator
    assert rule["noDataState"] == "OK", "没部署 coding-web 或没配 GitHub App 时不告警"
    assert "coding-web" in rule["annotations"]["summary"]


def test_the_backlog_alert_waits_for_a_sustained_backlog() -> None:
    assert rules()["coding-github-backlog"]["for"] == "15m", "处理一阵突发的投递不算积压"
