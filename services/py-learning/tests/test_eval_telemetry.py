"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 9.2、9.3 节验收（评测一侧）：评测运行也发 span 与指标。

细化见该计划第 11.9 节：
- run_eval 收一个 Telemetry（命令行设了 OTEL_EXPORTER_OTLP_ENDPOINT 时是 OpenTelemetry 实现，按平台作用域 evals 绑定）。
- 一次运行一个 eval.run span：eval.set、eval.subject、eval.split、eval.valid，以及数值指标（eval.success_rate、
  eval.triage_accuracy、eval.verdict_accuracy、eval.cost_usd）与用到的模型（eval.models，逗号分隔）；它下面每条用例一个
  eval.case span：eval.case_id、eval.kind、eval.passed、eval.cost_usd。留出集不发 eval.case（只给汇总，同报告）。
- 不带用户数据：用例的消息、Stage、Verdict 原文都不进属性。
- 指标：每次运行一个 runtime.eval.runs 计数，每个数值指标一次 runtime.eval.score 观测（属性 eval.metric、eval.subject、
  eval.split、eval.valid）；Grafana 从 VictoriaMetrics 画趋势（第 9.3 节）。
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider

from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET, load_eval_set
from agentruntime_learning.evals.runner import run_eval
from agentruntime_learning.evals.subjects import load_subject
from agentruntime_learning.evals.worker_subject import WorkerPlannerSubject

from fake_gateway import ScriptedGateway


async def run_with_telemetry(split: str = "dev", subject: Any = None) -> tuple[Any, ConsoleTelemetryProvider]:
    es = load_eval_set(DEFAULT_EVAL_SET)
    provider = ConsoleTelemetryProvider(stream=io.StringIO())
    telemetry = provider.bind_platform("evals")
    report = await run_eval(es, subject or load_subject("baseline", es), split=split, reports_dir=None, compare=None, telemetry=telemetry)
    await telemetry.flush()
    return report, provider


async def test_a_run_is_one_eval_run_span_with_a_span_per_case() -> None:
    report, provider = await run_with_telemetry()
    [run] = [s for s in provider.exported_spans if s.name == "eval.run"]
    a = run.attributes
    assert (a["eval.set"], a["eval.subject"], a["eval.split"], a["eval.valid"]) == (report.data["eval_set"]["name"], "baseline", "dev", True)
    assert a["eval.success_rate"] == pytest.approx(report.data["metrics"]["success_rate"])
    assert "eval.cost_usd" in a and "eval.verdict_accuracy" in a
    cases = [s for s in provider.exported_spans if s.name == "eval.case"]
    assert len(cases) == len(report.data["cases"]) and all(c.parent_span_id == run.span_id for c in cases)
    by_id = {c.attributes["eval.case_id"]: c.attributes for c in cases}
    for case in report.data["cases"]:
        assert by_id[case["id"]]["eval.passed"] == case["passed"] and by_id[case["id"]]["eval.kind"] == case["kind"]


async def test_the_models_a_run_used_are_on_its_span() -> None:
    _, provider = await run_with_telemetry(subject=WorkerPlannerSubject(ScriptedGateway(vendor="vendor_m")))
    [run] = [s for s in provider.exported_spans if s.name == "eval.run"]
    assert run.attributes["eval.valid"] is False, "规划全回退的运行无效（第 8 节）"
    assert all(m.startswith("vendor_m/") for m in run.attributes["eval.models"].split(","))


async def test_holdout_runs_send_no_case_spans() -> None:
    _, provider = await run_with_telemetry("holdout")
    assert [s.name for s in provider.exported_spans if s.name.startswith("eval.")] == ["eval.run"]


async def test_spans_carry_no_case_content() -> None:
    _, provider = await run_with_telemetry()
    es = load_eval_set(DEFAULT_EVAL_SET)
    messages = [c.planner_input["message"] for c in es.only("dev").task_cases]
    dumped = json.dumps([s.attributes for s in provider.exported_spans], ensure_ascii=False)
    assert not [m for m in messages if m and m in dumped], "用例的消息原文不进 span"


async def test_a_run_records_its_scores_as_metrics() -> None:
    report, provider = await run_with_telemetry()
    runs = [m for m in provider.exported_metrics if m.name == "runtime.eval.runs"]
    assert len(runs) == 1 and runs[0].attributes["eval.subject"] == "baseline"
    scores = {m.attributes["eval.metric"]: m.value for m in provider.exported_metrics if m.name == "runtime.eval.score"}
    assert scores["success_rate"] == pytest.approx(report.data["metrics"]["success_rate"])
    assert {"verdict_accuracy", "cost_usd"} <= set(scores)
    assert all(m.attributes["eval.valid"] is True for m in provider.exported_metrics if m.name == "runtime.eval.score")
