"""DISK-A 验收（WS-DISK-01 ③，docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 2 节）：工作区卷用量告警。

挂卷的工作区放在节点的本地盘上（deploy/cluster/workspaces.yaml 的 hostPath），盘满会连带同节点上的 Postgres、Temporal。
Worker 的对账每一轮报一次卷的已用比例（runtime.workspace.volume_used_ratio，Telemetry 的 observe：VictoriaMetrics 里是
_sum 与 _count），看板上有这一栏，超过 80% 告警。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

DATA = Path(__file__).resolve().parents[2] / "deploy" / "data"
EXPR = "max(increase(runtime_workspace_volume_used_ratio_sum[15m]) / increase(runtime_workspace_volume_used_ratio_count[15m]))"


def rules() -> dict[str, dict[str, Any]]:
    values = yaml.safe_load((DATA / "values" / "grafana.yaml").read_text("utf-8"))
    return {r["uid"]: r for g in values["alerting"]["rules.yaml"]["groups"] for r in g["rules"]}


def runtime_board() -> dict[str, Any]:
    for cm in yaml.safe_load_all((DATA / "dashboards" / "agent-runtime.yaml").read_text("utf-8")):
        for raw in cm["data"].values():
            return json.loads(raw)
    raise AssertionError("agent-runtime.yaml 里没有看板")


def test_volume_usage_alerts_above_eighty_percent() -> None:
    rule = rules()["ar-workspace-volume-usage"]
    assert rule["title"] == "工作区卷用量" and rule["labels"]["severity"] == "critical"
    [query] = [d for d in rule["data"] if d.get("datasourceUid") == "victoriametrics"]
    assert query["model"]["expr"] == EXPR
    [threshold] = [d for d in rule["data"] if d.get("model", {}).get("type") == "threshold"]
    assert threshold["model"]["conditions"][0]["evaluator"] == {"type": "gt", "params": [0.8]}
    assert "80%" in rule["annotations"]["summary"]
    assert rule["noDataState"] == "OK", "没有挂卷的部署（只有存档模式）不告警"


def test_the_runtime_board_shows_the_volume_usage() -> None:
    panels = {p["title"]: p for p in runtime_board()["panels"]}
    panel = panels["工作区卷用量"]
    assert [t["expr"] for t in panel["targets"]] == [EXPR]
    assert panel["fieldConfig"]["defaults"]["unit"] == "percentunit"
