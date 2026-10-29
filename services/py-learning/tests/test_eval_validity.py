"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 8 节验收：评测本身的可信度。

细化见该计划第 11.9 节：
- 报告记下每个 role 实际用到的 model_profile_id（models：{role: {model_profile_id: 次数}}，role 取 CallContext.role 的小写名，
  例如 route、plan、judge）：2026-10-28 的“退步”是靠时间线对出来的，以后看报告就知道。
- 报告记下 Planner、Reviewer 回退成规则的次数（metrics.fallbacks：{planner, reviewer, decisions, rate}）；回退率超过阈值
  （默认 5%，run_eval 的 max_fallback_rate、命令行 --max-fallback-rate）的运行标为无效（valid = false，invalid_reason 说明），
  摘要第一行写明“无效”。不经模型的被测对象（baseline、oracle）没有回退，一律有效。
- 重复跑（--repeat）的汇总：中位数只取有效的运行，values 仍列出每一次；invalid_runs 记无效的次数。
- S-11 的压缩评测：一条样本出错（例如模型给的 JSON 解析不了）只记这一条失败（行里带 error，指标多一项 errors），整轮继续。
- 两条全错的 Reviewer 样本（rs_debate_no_convergence、rs_judge_bias）请人复核标注：不是代码，不在这里测。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from agentruntime.common.v1 import enums_pb2

from agentruntime_learning.evals.__main__ import main
from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET, load_eval_set
from agentruntime_learning.evals.context import STRATEGIES, load_context_samples, run_context_eval
from agentruntime_learning.evals.repeat import aggregate, run_repeated
from agentruntime_learning.evals.runner import Report, run_eval
from agentruntime_learning.evals.subjects import load_subject
from agentruntime_learning.evals.worker_subject import ENV_GATEWAY_FACTORY, WorkerPlannerSubject

from context_support import CompactionGateway, ScriptedContextSubject, context_sample, write_samples
from fake_gateway import ScriptedGateway


def role_name(role: int) -> str:
    return enums_pb2.Role.Name(role).removeprefix("ROLE_").lower()


# --- 每个 role 用的模型 ---


async def test_the_report_records_the_model_each_role_used() -> None:
    es = load_eval_set(DEFAULT_EVAL_SET)
    gateway = ScriptedGateway(vendor="vendor_m")
    report = await run_eval(es, WorkerPlannerSubject(gateway), reports_dir=None, compare=None)
    expected: dict[str, Counter[str]] = {}
    for r in gateway.requests:
        expected.setdefault(role_name(r.call.role), Counter())[f"vendor_m/{r.call.tier}"] += 1
    assert report.data["models"] == {role: dict(c) for role, c in expected.items()}
    assert {"route", "plan"} <= set(report.data["models"])


async def test_a_subject_without_a_model_records_no_models() -> None:
    es = load_eval_set(DEFAULT_EVAL_SET)
    report = await run_eval(es, load_subject("baseline", es), reports_dir=None, compare=None)
    assert report.data.get("models", {}) == {}


# --- 回退与无效 ---


async def test_fallbacks_are_counted_and_too_many_make_the_run_invalid() -> None:
    """规划的模型每次都答不合格：每条 Task 用例都回退到保守规划，回退率 100%，这次运行无效。"""
    es = load_eval_set(DEFAULT_EVAL_SET)
    report = await run_eval(es, WorkerPlannerSubject(ScriptedGateway()), reports_dir=None, compare=None)
    fallbacks = report.data["metrics"]["fallbacks"]
    cases = len(es.only("dev").task_cases)
    assert fallbacks["planner"] == cases and fallbacks["reviewer"] == 0
    assert fallbacks["decisions"] == cases and fallbacks["rate"] == 1.0, "baseline 的 Reviewer 不经模型，不算进分母"
    assert report.data["valid"] is False and "回退" in report.data["invalid_reason"]
    assert "无效" in report.summary().splitlines()[0]


async def test_a_subject_without_a_model_is_always_valid() -> None:
    es = load_eval_set(DEFAULT_EVAL_SET)
    report = await run_eval(es, load_subject("baseline", es), reports_dir=None, compare=None)
    assert report.data["valid"] is True and "invalid_reason" not in report.data
    assert "无效" not in report.summary()


async def test_the_fallback_threshold_is_configurable() -> None:
    es = load_eval_set(DEFAULT_EVAL_SET)
    report = await run_eval(es, WorkerPlannerSubject(ScriptedGateway()), reports_dir=None, compare=None, max_fallback_rate=1.0)
    assert report.data["valid"] is True and report.data["max_fallback_rate"] == 1.0


def test_the_cli_passes_the_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv(ENV_GATEWAY_FACTORY, "fake_gateway:make")
    argv = ["run", "--subject", "agentruntime_learning.evals.worker_subject:planner_subject", "--reports-dir", str(tmp_path), "--compare", "none"]
    main([*argv, "--max-fallback-rate", "0.25"])
    [written] = [p for p in tmp_path.glob("*.json") if not p.name.startswith("repeat--")]
    data = json.loads(written.read_text("utf-8"))
    assert data["max_fallback_rate"] == 0.25 and data["valid"] is False
    assert "无效" in capsys.readouterr().out


# --- 重复跑：中位数只取有效的 ---


def fake_report(success_rate: float, *, valid: bool) -> Report:
    return Report(data={"metrics": {"success_rate": success_rate}, "failure_reasons": [], "valid": valid})


def test_the_median_of_repeated_runs_skips_invalid_runs() -> None:
    runs = [fake_report(0.8, valid=True), fake_report(0.1, valid=False), fake_report(0.9, valid=True)]
    entry = aggregate(runs)["success_rate"]
    assert entry["values"] == [0.8, 0.1, 0.9], "每一次都列出来"
    assert entry["median"] == pytest.approx(0.85) and (entry["min"], entry["max"]) == (0.8, 0.9)


async def test_a_repeated_run_counts_its_invalid_runs() -> None:
    es = load_eval_set(DEFAULT_EVAL_SET)
    repeated = await run_repeated(es, WorkerPlannerSubject(ScriptedGateway()), repeat=2, reports_dir=None, compare=None)
    assert repeated.data["invalid_runs"] == 2
    assert repeated.data["metrics"]["success_rate"]["median"] is None, "没有有效的运行就没有中位数"


# --- S-11：一条样本出错不让整轮崩 ---


class BrokenOnSecondSample(ScriptedContextSubject):
    """每种策略的第二条样本答题时抛出解析错误（真模型给了不合法的 JSON）。"""

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.calls = 0

    async def answer(self, messages: Any, questions: Any) -> list[str]:
        self.calls += 1
        if self.calls % 2 == 0:
            raise json.JSONDecodeError("Expecting value", "not json", 0)
        return await super().answer(messages, questions)


async def test_a_sample_that_breaks_fails_alone(tmp_path: Path) -> None:
    root = write_samples(tmp_path / "set", [context_sample("c1"), context_sample("c2")])
    subject = BrokenOnSecondSample(CompactionGateway(), answer_fn=lambda c, q: "", action_fn=lambda c: ("repo_read", {"path": "a.py"}))
    report = await run_context_eval(load_context_samples(root), subject, split="dev", work_dir=tmp_path / "work")
    for strategy in STRATEGIES:
        assert report.metrics[strategy]["samples"] == 2 and report.metrics[strategy]["errors"] == 1
    broken = [row for row in report.samples if row["id"] == "c2"]
    assert len(broken) == len(STRATEGIES) and all("JSONDecodeError" in row["error"] for row in broken)
    assert all("error" not in row for row in report.samples if row["id"] == "c1")
