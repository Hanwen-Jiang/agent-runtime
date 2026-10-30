"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 11.12 节：评测看板（Eval Dashboard）的公开版。

scripts/eval_dashboard.py --public 生成对外开放的页面：只有评测集上的汇总，不带生产数据（网关计量与 span 里有租户、Task id 与
每个 Task 的花费）。给了生产数据的输入就拒绝；标题写英文与中文两个名字，每节标题中英文都有，开头说明数字怎么来的；不连外网；
超过 ConfigMap 的上限就报错。不加 --public 时内部版不变。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
HOLDOUT_ONLY_REASON = "holdout_only_reason"
REPORT_FILE = "task-loop--worker--dev--20261028T000000Z.json"


@pytest.fixture(scope="module")
def dashboard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("eval_dashboard_under_test", REPO / "scripts" / "eval_dashboard.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def report(split: str, reason: str) -> dict[str, Any]:
    return {
        "subject": "worker",
        "split": split,
        "created_at": "2026-10-28T00:00:00Z",
        "metrics": {
            "planner": {"triage_accuracy": 0.92, "shape_match_rate": 0.8, "decisions": 10},
            "reviewer": {"verdict_accuracy": 0.87, "decisions": 10},
            "rounds": {"verdict_accuracy": 0.9, "decisions": 5},
            "cost": {"total_usd": 1.5, "per_case_usd": 0.05},
        },
        "cases": [{"kind": "task", "passed": True}],
        "failure_reasons": [{"dimension": "verdict", "code": reason, "count": 3}],
    }


@pytest.fixture
def reports(tmp_path: Path) -> Path:
    root = tmp_path / "reports"
    group = root / "exp-2026-10-28" / "e2-opus"
    group.mkdir(parents=True)
    (group / REPORT_FILE).write_text(json.dumps(report("dev", "stuck_retry")), "utf-8")
    (group / "task-loop--worker--holdout--20261028T000000Z.json").write_text(json.dumps(report("holdout", HOLDOUT_ONLY_REASON)), "utf-8")
    (group / "context-worker-context-dev-20261028T000000Z.json").write_text(
        json.dumps({"split": "dev", "gate_passed": True, "metrics": {"full": {"probe_pass_rate": 0.95, "retention_rate": 0.95, "repeat_rate": 0.0}, "mask_only": {"probe_pass_rate": 0.7}}}),
        "utf-8",
    )
    return root


def public_page(dashboard: ModuleType, reports: Path, tmp_path: Path) -> str:
    out = tmp_path / "index.html"
    assert dashboard.main(["--public", "--reports", str(reports), "--out", str(out)]) == 0
    return out.read_text("utf-8")


def embedded_data(page: str) -> dict[str, Any]:
    match = re.search(r"const D=(\{.*?\});const C=", page, re.S)
    assert match, "页面里有内嵌的数据"
    return json.loads(match.group(1).replace("<\\/", "</"))


def test_the_public_page_carries_both_names(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    page = public_page(dashboard, reports, tmp_path)
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    assert title and "Eval Dashboard" in title.group(1) and "评测看板" in title.group(1)
    h1 = re.search(r"<h1>(.*?)</h1>", page, re.S)
    assert h1 and "Eval Dashboard" in h1.group(1) and "评测看板" in h1.group(1)


def test_every_public_section_heading_is_in_both_languages(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    headings = re.findall(r"<h2>(.*?)</h2>", public_page(dashboard, reports, tmp_path), re.S)
    assert headings, "页面有分节"
    for heading in headings:
        assert re.search(r"[A-Za-z]{3}", heading) and re.search(r"[一-鿿]", heading), f"标题要有英文与中文：{heading}"


def test_the_public_page_says_what_the_numbers_are(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    page = public_page(dashboard, reports, tmp_path)
    about = re.search(r'<[a-z]+[^>]*id="about"[^>]*>(.*?)</[a-z]+>', page, re.S)
    assert about, "开头有一段说明"
    text = about.group(1)
    assert "internal eval sets" in text and "内部评测集" in text, "说明数据来自内部评测集，不是用户的数据"
    assert "relay" in text and "中转" in text, "说明模型调用经第三方中转，成本按它报的用量算"


def test_the_public_page_has_only_the_eval_sections(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    page = public_page(dashboard, reports, tmp_path)
    for section in ("trend", "scatter", "runs", "failures", "context"):
        assert f'id="{section}"' in page, f"评测的这一节要在：{section}"
    for section in ("metering", "latency", "tasks", "tools"):
        assert f'id="{section}"' not in page, f"运行指标不公开：{section}"
        assert f"getElementById('{section}')" not in page, f"脚本不能再引用去掉的节，否则页面报错：{section}"
    assert "D.metering" not in page and "D.spans" not in page and "span_sources" not in page
    for words in ("网关计量", "真 Task", "工具调用"):
        assert words not in page


def test_the_public_data_holds_only_eval_summaries(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    page = public_page(dashboard, reports, tmp_path)
    data = embedded_data(page)
    assert set(data) == {"generated_at", "reports"}, "只有生成时间与评测的汇总"
    assert data["reports"]["runs"], "运行都在"
    assert all("file" not in r for r in [*data["reports"]["runs"], *data["reports"]["contexts"]]), "不带报告的文件名"
    assert REPORT_FILE not in page
    assert "tenant" not in page and "tsk_" not in page


def test_holdout_stays_a_summary_on_the_public_page(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    page = public_page(dashboard, reports, tmp_path)
    assert {r["split"] for r in embedded_data(page)["reports"]["runs"]} == {"dev", "holdout"}, "留出集的汇总照样显示"
    assert HOLDOUT_ONLY_REASON not in page, "留出集的失败原因不出现"


@pytest.mark.parametrize(("flag", "prefix"), [("--metering", "live="), ("--spans", "")], ids=["metering", "spans"])
def test_the_public_build_refuses_production_inputs(dashboard: ModuleType, reports: Path, tmp_path: Path, flag: str, prefix: str) -> None:
    source = tmp_path / "production.jsonl"
    source.write_text("", "utf-8")
    out = tmp_path / "index.html"
    with pytest.raises(SystemExit) as info:
        dashboard.main(["--public", "--reports", str(reports), "--out", str(out), flag, f"{prefix}{source}"])
    assert info.value.code == 2
    assert not out.exists(), "拒绝时不生成页面"


def test_the_public_page_makes_no_external_requests(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    page = public_page(dashboard, reports, tmp_path)
    assert not re.search(r"<(script|link|img|iframe)[^>]+(src|href)=", page), "不引用外部资源"
    urls = set(re.findall(r"https?://[^\s\"'`<>)]+", page))
    assert urls <= {"http://www.w3.org/2000/svg"}, f"除了 SVG 的命名空间没有别的地址：{urls}"


def test_a_page_too_big_for_a_configmap_is_refused(dashboard: ModuleType, reports: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard, "MAX_PUBLIC_BYTES", 100)
    out = tmp_path / "index.html"
    with pytest.raises(SystemExit) as info:
        dashboard.main(["--public", "--reports", str(reports), "--out", str(out)])
    assert info.value.code == 2
    assert not out.exists()


def test_the_internal_page_is_unchanged(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    out = tmp_path / "internal.html"
    metering = tmp_path / "metering.jsonl"
    metering.write_text(json.dumps({"model_profile_id": "a/m@1", "cost_usd_micros": 1000, "status": "ok", "tenant_id": "t1"}) + "\n", "utf-8")
    assert dashboard.main(["--reports", str(reports), "--metering", f"live={metering}", "--out", str(out)]) == 0
    page = out.read_text("utf-8")
    data = embedded_data(page)
    assert {"metering", "metering_sources", "spans", "span_sources"} <= set(data)
    assert data["metering"] and all("file" in r for r in data["reports"]["runs"])
    assert '<h2>网关计量：按模型的花费与失败</h2>' in page and 'id="tools"' in page
