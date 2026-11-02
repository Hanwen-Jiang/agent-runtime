"""docs/plan/eval-observability-and-runtime-fixes.md 第 11.16 节：评测边跑边发指标（agentruntime_learning.evals.live）。

- 开始发 runtime.eval.cases_planned（这次要跑几条）；每条用例结束发一次 runtime.eval.cases（eval.outcome：passed、failed、error）
  与 runtime.eval.case_cost_usd；结束发 runtime.eval.score 与 runtime.eval.finished。
- 共同的属性：eval.suite、eval.run_id、eval.process、eval.subject、eval.split（结束时另有 eval.model）；只有名字与数字，没有用例原文与 id。
- 留出集只发开始与结束的汇总；没有 Telemetry 时一条不发。S-11 的压缩评测同样发（suite 为 context）。
"""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider

from agentruntime_learning.evals import live as lv
from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET, load_eval_set
from agentruntime_learning.evals.context import STRATEGIES, load_context_samples, load_context_subject, run_context_eval
from agentruntime_learning.evals.runner import run_eval
from agentruntime_learning.evals.subjects import load_subject

COMMON = {"eval.suite", "eval.run_id", "eval.process", "eval.subject", "eval.split"}


def provider() -> ConsoleTelemetryProvider:
    return ConsoleTelemetryProvider(stream=io.StringIO())


async def run_task_loop(split: str = "dev") -> tuple[Any, ConsoleTelemetryProvider]:
    es = load_eval_set(DEFAULT_EVAL_SET)
    p = provider()
    telemetry = p.bind_platform("evals")
    report = await run_eval(es, load_subject("baseline", es), split=split, reports_dir=None, compare=None, telemetry=telemetry)
    await telemetry.flush()
    return report, p


def named(p: ConsoleTelemetryProvider, name: str) -> list[Any]:
    return [m for m in p.exported_metrics if m.name == name]


async def test_a_run_reports_its_plan_each_case_and_its_end() -> None:
    report, p = await run_task_loop()
    names = [m.name for m in p.exported_metrics if m.name.startswith("runtime.eval.")]
    assert names[0] == lv.PLANNED, "先说这次要跑几条"
    assert names[-1] == lv.FINISHED, "最后说跑完了"
    [planned] = named(p, lv.PLANNED)
    assert planned.value == len(report.data["cases"])
    cases = named(p, lv.CASES)
    assert len(cases) == len(report.data["cases"]), "每条用例一次"
    outcomes = [c.attributes["eval.outcome"] for c in cases]
    assert outcomes.count("passed") == sum(1 for c in report.data["cases"] if c["passed"])
    assert set(outcomes) <= {"passed", "failed", "error"}
    assert len(named(p, lv.CASE_COST)) == len(cases)
    run_ids = {m.attributes["eval.run_id"] for m in p.exported_metrics if m.name.startswith("runtime.eval.")}
    assert run_ids == {report.data["run_id"]}, "一次运行一个 id，报告里也记着"
    for m in p.exported_metrics:
        if m.name.startswith("runtime.eval."):
            assert COMMON <= set(m.attributes), (m.name, m.attributes)
            assert m.attributes["eval.suite"] == "task-loop" and m.attributes["eval.subject"] == "baseline" and m.attributes["eval.split"] == "dev"
    scores = {m.attributes["eval.metric"] for m in named(p, lv.SCORE)}
    assert {"success_rate", "cost_usd"} <= scores
    assert all(m.attributes["eval.run_id"] == report.data["run_id"] for m in named(p, lv.SCORE)), "看板按 run_id 把分数对上"


async def test_metrics_carry_no_case_content_or_ids() -> None:
    report, p = await run_task_loop()
    es = load_eval_set(DEFAULT_EVAL_SET)
    dumped = json.dumps([m.attributes for m in p.exported_metrics], ensure_ascii=False)
    messages = [c.planner_input["message"] for c in es.only("dev").task_cases]
    assert not [m for m in messages if m and m in dumped], "用例的消息原文不进指标"
    assert not [c["id"] for c in report.data["cases"] if f'"{c["id"]}"' in dumped], "用例 id 也不进指标（只有 span 带）"


async def test_holdout_sends_only_the_summary() -> None:
    _, p = await run_task_loop("holdout")
    assert named(p, lv.PLANNED) and named(p, lv.FINISHED) and named(p, lv.SCORE)
    assert not named(p, lv.CASES) and not named(p, lv.CASE_COST), "留出集不发逐条的"


async def test_without_telemetry_nothing_is_sent() -> None:
    run = lv.LiveRun(None, suite="task-loop", subject="baseline", split="dev")
    await run.planned(3)
    await run.case("passed", 0.1)
    await run.finished()
    es = load_eval_set(DEFAULT_EVAL_SET)
    report = await run_eval(es, load_subject("baseline", es), split="dev", reports_dir=None, compare=None, telemetry=None)
    assert report.data["run_id"]


async def test_an_unknown_outcome_is_refused() -> None:
    with pytest.raises(ValueError):
        await lv.LiveRun(provider().bind_platform("evals"), suite="x", subject="y", split="dev").case("maybe")


async def test_a_failing_exporter_does_not_fail_the_eval() -> None:
    class Broken:
        async def count(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("collector down")

        async def observe(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("collector down")

    run = lv.LiveRun(Broken(), suite="task-loop", subject="s", split="dev")  # type: ignore[arg-type]
    await run.planned(1)
    await run.case("failed", 0.2)
    await run.finished()


def test_run_ids_are_distinct_and_safe() -> None:
    ids = {lv.new_run_id("worker:worker_subject") for _ in range(50)}
    assert len(ids) == 50
    assert all(all(ch.isalnum() or ch in "-_.TZ" for ch in i) for i in ids)


def test_the_blocking_wrapper_works_from_worker_threads() -> None:
    p = provider()
    live = lv.BlockingLive(lv.LiveRun(p.bind_platform("evals"), suite="swebench", subject="m", split="verified", run_id="r1"))
    live.planned(4, carried=1, carried_cost_usd=0.5)
    threads = [threading.Thread(target=live.case, args=("submitted", 0.25)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    live.finished(model="gpt-x")
    live.flush()
    live.close()
    assert [m.value for m in named(p, lv.PLANNED)] == [4]
    assert [m.value for m in named(p, "runtime.eval.cases_carried")] == [1]
    assert len(named(p, lv.CASES)) == 3
    [fin] = named(p, lv.FINISHED)
    assert fin.attributes["eval.model"] == "gpt-x" and fin.attributes["eval.run_id"] == "r1"


async def test_the_context_eval_reports_progress(tmp_path: Path) -> None:
    samples = load_context_samples(DEFAULT_EVAL_SET)
    p = provider()
    telemetry = p.bind_platform("evals")
    report = await run_context_eval(samples, load_context_subject("lexical"), split="dev", work_dir=tmp_path, telemetry=telemetry)
    await telemetry.flush()
    dev = [s for s in samples if s.split == "dev"]
    [planned] = named(p, lv.PLANNED)
    assert planned.value == len(STRATEGIES) * len(dev) and planned.attributes["eval.suite"] == "context"
    assert len(named(p, lv.CASES)) == len(STRATEGIES) * len(dev)
    scores = {m.attributes["eval.metric"]: m.value for m in named(p, lv.SCORE)}
    assert scores["full.probe_pass_rate"] == pytest.approx(report.metrics["full"]["probe_pass_rate"])
    assert len(named(p, lv.FINISHED)) == 1
