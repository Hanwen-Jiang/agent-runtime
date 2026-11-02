"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 11.12 节：评测看板（Eval Dashboard）的公开版。

scripts/eval_dashboard.py --public 生成对外开放的页面：只有评测集上的汇总，不带生产数据（网关计量与 span 里有租户、Task id 与
每个 Task 的花费）。给了生产数据的输入就拒绝；标题写英文与中文两个名字，每节标题中英文都有，开头说明数字怎么来的；不连外网；
超过 ConfigMap 的上限就报错。不加 --public 时内部版不变。

第 11.12 节的补充（2026-10-31，人定）：读者不懂这个项目也要看得懂（例如 HR）：说清在做什么、怎么考、发现了什么，每张图说明怎么看，
术语有解释，结论来自报告，也写明还没做的。
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
    about = re.search(r'<section[^>]*id="about"[^>]*>(.*?)</section>', page, re.S)
    assert about, "有一节说明数字怎么来的"
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


def test_the_public_page_has_a_live_section(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    """第 11.16 节：实时的一栏每 30 秒从同源的 /api/live.json 取一次；取不到时写明显示的是静态数据。"""
    page = public_page(dashboard, reports, tmp_path)
    assert 'id="sec-live"' in page and 'id="live"' in page
    assert "fetch('/api/live.json'" in page and "setInterval(load,30000)" in page
    assert "最后一次发布的静态数据" in page and "last published snapshot" in page
    assert page.index('id="sec-live"') < page.index('id="sec-trend"'), "实时的一栏在前面"


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


def section(page: str, section_id: str) -> str:
    found = re.search(rf'<section[^>]*id="{section_id}"[^>]*>(.*?)</section>', page, re.S)
    assert found, f"页面有这一节：{section_id}"
    return found.group(1)


def has_both_languages(html: str) -> bool:
    text = re.sub(r"<[^>]+>", " ", html)
    return bool(re.search(r"[A-Za-z]{3}", text) and re.search(r"[一-鿿]", text))


@pytest.mark.parametrize("section_id", ["summary", "what", "how", "findings", "glossary", "next", "about"])
def test_the_public_page_explains_itself_to_a_reader_outside_the_project(dashboard: ModuleType, reports: Path, tmp_path: Path, section_id: str) -> None:
    assert has_both_languages(section(public_page(dashboard, reports, tmp_path), section_id))


@pytest.mark.parametrize("section_id", ["sec-trend", "sec-scatter", "sec-failures", "sec-context", "sec-runs"])
def test_every_chart_says_how_to_read_it(dashboard: ModuleType, reports: Path, tmp_path: Path, section_id: str) -> None:
    body = section(public_page(dashboard, reports, tmp_path), section_id)
    how = re.findall(r'<(?:p|div)[^>]*class="how[^"]*"[^>]*>(.*?)</(?:p|div)>', body, re.S)
    assert how and has_both_languages(" ".join(how)), "每张图都说明它在说什么、怎么看"


def test_the_summary_comes_from_the_reports(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    summary = section(public_page(dashboard, reports, tmp_path), "summary")
    assert "92.0%" in summary and "87.0%" in summary, "最新一次的成绩"
    assert "90%" in summary and "85%" in summary, "及格线"
    assert "还没过线" not in summary, "这份数据两项都过了"


def test_the_summary_says_when_a_gate_is_not_met(dashboard: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "low"
    group = root / "exp-2026-10-28" / "e5-fixed-v2"
    group.mkdir(parents=True)
    low = report("dev", "stuck_retry")
    low["metrics"]["reviewer"]["verdict_accuracy"] = 0.5625
    (group / REPORT_FILE).write_text(json.dumps(low), "utf-8")
    summary = section(public_page(dashboard, root, tmp_path), "summary")
    assert "56.3%" in summary and "还没过线" in summary and "not passed" in summary.lower()


def test_a_run_that_fell_back_to_rules_does_not_set_the_headline(dashboard: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "invalid"
    good, bad = root / "a" / "good", root / "b" / "bad"
    good.mkdir(parents=True)
    bad.mkdir(parents=True)
    (good / "task-loop--worker--dev--20261028T000000Z.json").write_text(json.dumps(report("dev", "x")), "utf-8")
    invalid = report("dev", "x") | {"created_at": "2026-10-29T00:00:00Z", "model_call_failure_codes": {"models_exhausted": 20}}
    invalid["metrics"] = {**invalid["metrics"], "reviewer": {"verdict_accuracy": 0.11, "decisions": 10}}
    (bad / "task-loop--worker--dev--20261029T000000Z.json").write_text(json.dumps(invalid), "utf-8")
    summary = section(public_page(dashboard, root, tmp_path), "summary")
    assert "87.0%" in summary and "11.0%" not in summary


def test_the_exam_size_comes_from_the_eval_set(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    root = REPO / "services" / "py-learning" / "evalsets" / "task-loop"
    total = sum(1 for sub in ("task_cases", "reviewer_samples", "context_samples") for _ in (root / sub).glob("*.json"))
    assert f"{total}" in section(public_page(dashboard, reports, tmp_path), "how")


def test_readers_see_names_not_experiment_ids(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    page = public_page(dashboard, reports, tmp_path)
    [run] = [r for r in embedded_data(page)["reports"]["runs"] if r["split"] == "dev"]
    assert run["label_en"] == "Claude Opus" and run["label_zh"] == "Claude Opus"


@pytest.mark.parametrize("code", [
    "verdict/verdict", "shape/modes", "shape/deliver", "escalation/stuck_retry", "shape/quality", "shape/nodes", "shape/gate",
    "shape/fan_out", "triage/triage", "shape/forbidden_roles", "shape/human",
])
def test_every_failure_reason_has_a_plain_name(dashboard: ModuleType, code: str) -> None:
    zh, en = dashboard.FAILURE_TEXT[code]
    assert re.search(r"[一-鿿]", zh) and re.search(r"[A-Za-z]{3}", en)
    assert code not in zh and code not in en


@pytest.mark.parametrize("term", ["Planner", "Stage", "Reviewer", "Triage", "Verdict", "Round", "Token", "开发集", "留出集", "及格线", "回退", "中转"])
def test_the_glossary_covers_the_terms_on_the_page(dashboard: ModuleType, reports: Path, tmp_path: Path, term: str) -> None:
    assert term in section(public_page(dashboard, reports, tmp_path), "glossary")


@pytest.mark.parametrize("name", ["SWE-bench", "Terminal-Bench", "DeepEval", "Ragas"])
def test_the_page_says_what_has_not_run_yet(dashboard: ModuleType, reports: Path, tmp_path: Path, name: str) -> None:
    row = next((r for r in re.findall(r"<tr>(.*?)</tr>", section(public_page(dashboard, reports, tmp_path), "next"), re.S) if name in r), "")
    assert row and has_both_languages(row), f"{name} 有一行说明现状"
    assert "%" not in row, "还没有成绩的，不写分数"


def test_the_page_opens_in_chinese_and_can_switch(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    page = public_page(dashboard, reports, tmp_path)
    assert re.search(r'<html[^>]*lang="zh-CN"[^>]*data-lang="zh"', page)
    assert 'data-set-lang="en"' in page and 'data-set-lang="zh"' in page
    script = "".join(re.findall(r"<script>(.*?)</script>", page, re.S))
    assert re.search(r"try\{[^}]*localStorage", script), "浏览器存储读写失败时页面照常显示"


def test_the_internal_page_has_none_of_the_public_explanations(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    out = tmp_path / "internal.html"
    assert dashboard.main(["--reports", str(reports), "--out", str(out)]) == 0
    page = out.read_text("utf-8")
    assert 'id="glossary"' not in page and 'data-set-lang' not in page


def bench_report(run_id: str = "gpt55-base50", selected: int = 50, resolved: int = 32) -> dict[str, Any]:
    return {
        "benchmark": "swe-bench", "run_id": run_id, "model_name": "agent-runtime-gpt-5.5", "selected": selected, "ran": selected, "resolved": resolved,
        "resolve_rate": resolved / selected, "graded": True, "graded_complete": True, "cost_usd": 27.11, "cost_per_instance_usd": 0.54, "cost_per_resolved_usd": 0.85,
        "rounds": {"median": 4.0, "mean": 4.5, "max": 10}, "settings": {"environment": "official"},
        "selection": {"instances_file": "/opt/swebench/data/verified.jsonl", "seed": 0},
        "by_repo": {"django/django": {"selected": 23, "ran": 23, "resolved": 17}, "pydata/xarray": {"selected": 2, "ran": 2, "resolved": 0}},
        "by_difficulty": {"<15 min fix": {"selected": 18, "ran": 18, "resolved": 17}, "1-4 hours": {"selected": 3, "ran": 3, "resolved": 0}},
        "instances": [{"instance_id": "django__django-1", "grading": "resolved", "status": "done"}, {"instance_id": "django__django-2", "grading": "unresolved", "status": "done"}],
    }


@pytest.fixture
def with_benchmark(reports: Path) -> Path:
    folder = reports / "benchmarks"
    folder.mkdir()
    (folder / "gpt55-base50.json").write_text(json.dumps(bench_report()), "utf-8")
    (folder / "gpt55-smoke5.json").write_text(json.dumps(bench_report("gpt55-smoke5", 5, 0)), "utf-8")
    return reports


def test_the_benchmark_section_comes_from_the_report(dashboard: ModuleType, with_benchmark: Path, tmp_path: Path) -> None:
    body = section(public_page(dashboard, with_benchmark, tmp_path), "sec-swebench")
    assert "32/50" in body and "64.0%" in body and "$0.54" in body
    assert "django/django" in body and "17/23" in body and "0/3" in body, "按仓库与按难度"
    assert has_both_languages(body)
    how = re.findall(r'<p class="how[^"]*"[^>]*>(.*?)</p>', body, re.S)
    assert how and has_both_languages(" ".join(how))


def test_the_benchmark_says_how_far_to_trust_it(dashboard: ModuleType, with_benchmark: Path, tmp_path: Path) -> None:
    """50 题的抽样误差不小；这不是全部题目、不是榜单成绩，经过了中转。"""
    body = section(public_page(dashboard, with_benchmark, tmp_path), "sec-swebench")
    assert "50%" in body and "76%" in body, "95% 区间（Wilson）"
    assert "榜单" in body and "leaderboard" in body and "中转" in body and "relay" in body


def test_earlier_small_runs_are_listed_but_do_not_set_the_headline(dashboard: ModuleType, with_benchmark: Path, tmp_path: Path) -> None:
    page = public_page(dashboard, with_benchmark, tmp_path)
    body = section(page, "sec-swebench")
    assert "0/5" in body, "小规模的试跑也列出来"
    assert "32/50" in section(page, "summary"), "开头的结论用规模最大的那次"


def test_the_public_page_does_not_carry_server_paths(dashboard: ModuleType, with_benchmark: Path, tmp_path: Path) -> None:
    page = public_page(dashboard, with_benchmark, tmp_path)
    assert "/opt/swebench" not in page and "instances_file" not in page
    data = embedded_data(page)["reports"]["benchmarks"]
    assert {b["run_id"] for b in data} == {"gpt55-base50", "gpt55-smoke5"}


def test_without_a_benchmark_there_is_no_section(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    page = public_page(dashboard, reports, tmp_path)
    assert 'id="sec-swebench"' not in page


def test_an_ungraded_run_shows_no_score(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    folder = reports / "benchmarks"
    folder.mkdir()
    (folder / "x.json").write_text(json.dumps({**bench_report(), "graded": False, "resolved": None, "resolve_rate": None}), "utf-8")
    page = public_page(dashboard, reports, tmp_path)
    assert 'id="sec-swebench"' not in page, "还没判分的不算成绩"


# --- 调优前后（TMP 计划第 11.15 节） ---


def tuned(run_id: str, solved: list[int], *, problems: range = range(50), cost: float = 0.6) -> dict[str, Any]:
    report = bench_report(run_id, len(problems), len(solved))
    report["cost_per_instance_usd"] = cost
    report["instances"] = [{"instance_id": f"p{i}", "grading": "resolved" if i in solved else "unresolved", "status": "done"} for i in problems]
    return report


def write(reports: Path, *runs: dict[str, Any]) -> Path:
    folder = reports / "benchmarks"
    folder.mkdir()
    for run in runs:
        (folder / f"{run['run_id']}.json").write_text(json.dumps(run), "utf-8")
    return reports


def tuning_of(dashboard: ModuleType, reports: Path, tmp_path: Path) -> list[dict[str, Any]]:
    return embedded_data(public_page(dashboard, reports, tmp_path))["reports"]["tuning"]


def test_tuning_runs_are_compared_problem_by_problem(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    """总数一样也可能是新解决了几题、又丢了几题，只看总数看不出来。"""
    write(reports, tuned("gpt55-base50", list(range(30))), tuned("gpt55-tune1", [*range(3, 30), 40, 41, 42]))
    base, one = tuning_of(dashboard, reports, tmp_path)
    assert base["run_id"] == "gpt55-base50" and "gained" not in base
    assert (one["resolved"], one["gained"], one["lost"]) == (30, 3, 3)
    assert one["same_problems"] is True and one["low"] < one["rate"] < one["high"]


def test_runs_on_other_problems_are_not_compared_with_the_baseline(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    write(reports, tuned("gpt55-base50", list(range(30))), tuned("gpt55-valid50", list(range(100, 125)), problems=range(100, 150)))
    base, valid = tuning_of(dashboard, reports, tmp_path)
    assert valid["run_id"] == "gpt55-valid50" and valid["same_problems"] is False and "gained" not in valid


def test_the_tuning_line_is_in_a_fixed_order_whatever_the_files_are_called(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    write(reports, tuned("gpt55-valid50", [1], problems=range(100, 150)), tuned("gpt55-tune2", [1]), tuned("gpt55-tune1", [1]), tuned("gpt55-base50", [1]), bench_report("gpt55-smoke5", 5, 0))
    assert [p["run_id"] for p in tuning_of(dashboard, reports, tmp_path)] == ["gpt55-base50", "gpt55-tune1", "gpt55-tune2", "gpt55-valid50"]


def test_the_chart_appears_once_there_is_something_to_compare(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    write(reports, tuned("gpt55-base50", list(range(30))))
    assert 'id="tuning"' not in public_page(dashboard, reports, tmp_path), "只有基线时没有可比的"
    (reports / "benchmarks" / "gpt55-tune1.json").write_text(json.dumps(tuned("gpt55-tune1", list(range(33)))), "utf-8")
    page = public_page(dashboard, reports, tmp_path)
    body = section(page, "sec-swebench")
    assert 'id="tuning"' in body and 'id="tuning-cost"' in body, "解决率与花费各一张图，不共用一个纵轴"
    assert has_both_languages(body) and "95%" in body


def test_the_headline_moves_to_the_unseen_problems_when_they_exist(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    write(reports, tuned("gpt55-base50", list(range(30))), tuned("gpt55-tune1", list(range(40))), tuned("gpt55-valid50", list(range(100, 125)), problems=range(100, 150)))
    assert "25/50" in section(public_page(dashboard, reports, tmp_path), "summary"), "没用过的题最说明问题，不用调过的题给自己打分"


def test_the_public_data_has_no_per_problem_lists(dashboard: ModuleType, reports: Path, tmp_path: Path) -> None:
    write(reports, tuned("gpt55-base50", list(range(30))), tuned("gpt55-tune1", list(range(33))))
    data = embedded_data(public_page(dashboard, reports, tmp_path))["reports"]["benchmarks"]
    assert all("problems" not in b and "solved" not in b for b in data)


def test_wilson_interval(dashboard: ModuleType) -> None:
    low, high = dashboard.wilson(32, 50)
    assert round(low, 2) == 0.50 and round(high, 2) == 0.76
    assert dashboard.wilson(0, 5)[0] == 0
