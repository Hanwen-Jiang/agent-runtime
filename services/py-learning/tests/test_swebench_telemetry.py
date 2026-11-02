"""docs/plan/eval-observability-and-runtime-fixes.md 第 11.16 节：SWE-bench 的适配器边跑边发指标（评测看板的实时一栏）。

- 开始时一共几题；续跑时另带之前跑完的题数与花费，不重发算过的题。
- 每题跑完一次：交了补丁是 submitted，补丁是空的是 failed，出错是 error；带这一题的花费。进程正常结束时 finished。
- 判分完发 resolved 与 resolved_rate。命令行设了 OTEL_EXPORTER_OTLP_ENDPOINT 才发，看板上的运行 id 是 run.json 的 run_id。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from agentruntime_learning.benchmarks import swebench as sb

from swebench_support import Crash, FakeRuntime, Script, rounds, settings, write_instances
from test_swebench import fake_harness, graded_run


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def planned(self, total: int, *, carried: int = 0, carried_cost_usd: float = 0.0) -> None:
        self.calls.append(("planned", (total, carried, round(carried_cost_usd, 6))))

    def case(self, outcome: str, cost_usd: float | None = None) -> None:
        self.calls.append(("case", (outcome, cost_usd)))

    def score(self, metric: str, value: float) -> None:
        self.calls.append(("score", (metric, value)))

    def finished(self, *, model: str = "") -> None:
        self.calls.append(("finished", model))

    def of(self, kind: str) -> list[Any]:
        return [v for k, v in self.calls if k == kind]


@pytest.fixture
def insts(tmp_path: Path) -> list[sb.Instance]:
    return sb.load_instances(write_instances(tmp_path / "instances.jsonl"))


def test_each_problem_is_reported_once_with_its_cost(tmp_path: Path, insts: list[sb.Instance]) -> None:
    fake = FakeRuntime({
        "acme__calc-1": Script(events=rounds("done"), cost_usd=0.1, diff="diff --git a/a b/a\n+1\n", files=["a"]),
        "acme__calc-2": Script(events=rounds("abort"), status="TASK_STATUS_ABORTED", final_verdict="VERDICT_ABORT", cost_usd=0.2),
    })
    live = Recorder()
    sb.run(fake, insts[:2], tmp_path / "out", settings(model_name="acme/agent"), live=live)
    assert live.calls[0] == ("planned", (2, 0, 0.0)), "先说一共几题"
    assert sorted(live.of("case")) == [("failed", 0.2), ("submitted", 0.1)], "交了补丁是 submitted，补丁空的是 failed"
    assert live.calls[-1] == ("finished", "acme/agent")


def test_resuming_does_not_report_finished_problems_again(tmp_path: Path, insts: list[sb.Instance]) -> None:
    out = tmp_path / "out"
    chosen = insts[:3]
    fake = FakeRuntime({
        "acme__calc-1": Script(cost_usd=0.3, diff="diff --git a/a b/a\n+1\n", files=["a"]),
        "acme__calc-2": Script(events=rounds("done"), cost_usd=0.3, crash_on_events=True),
        "beta__lib-3": Script(cost_usd=0.3),
    })
    first = Recorder()
    with pytest.raises(Crash):
        sb.run(fake, chosen, out, settings(budget_usd=1.0, task_usd_limit=0.35, concurrency=1), live=first)
    assert first.of("planned") == [(3, 0, 0.0)]
    assert not first.of("finished"), "崩了的进程不说跑完了"
    done_first = len(first.of("case"))

    second = Recorder()
    sb.run(fake, chosen, out, settings(budget_usd=1.0, task_usd_limit=0.35, concurrency=1), live=second)
    [(total, carried, carried_cost)] = second.of("planned")
    assert total == 3 and carried == done_first and carried_cost == pytest.approx(0.3 * done_first), "带上之前跑完的题数与花费"
    assert len(second.of("case")) == 3 - done_first, "之前算过的题不重发"
    assert second.of("finished")


def test_an_error_is_reported_as_error(tmp_path: Path, insts: list[sb.Instance]) -> None:
    class Broken(FakeRuntime):
        def diff(self, workspace_id: str, *, base: str) -> dict[str, Any]:
            raise ValueError("diff failed")

    live = Recorder()
    sb.run(Broken({"acme__calc-1": Script(cost_usd=0.1)}), insts[:1], tmp_path / "out", settings(), live=live)
    assert [o for o, _ in live.of("case")] == ["error"]


def test_grading_reports_the_resolved_rate(tmp_path: Path, insts: list[sb.Instance]) -> None:
    out = graded_run(tmp_path, insts)
    env = {"PYTHONPATH": str(fake_harness(tmp_path)), "FAKE_ARGV": str(tmp_path / "argv.json"), "FAKE_RESOLVED": "acme__calc-2", "FAKE_ERRORS": "beta__lib-3"}
    live = Recorder()
    sb.grade(out, python=sys.executable, env=env, live=live)
    assert dict(live.of("score")) == {"resolved": 1.0, "resolved_rate": 0.25}, "解决的题数与占选题（4 题）的比例"
