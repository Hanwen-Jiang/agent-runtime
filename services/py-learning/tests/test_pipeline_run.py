"""T-16：Learning Pipeline 的闭环（细化见 docs/plan/learning-pipeline.md）。

读 trace → 按租户过滤并留审计 → 归纳候选 Recipe → 观测式评测与门槛 → 过门槛才经 Registry 发布。
Registry 用记录调用的替身（pipeline_support.FakeRegistry）；真的 Registry 上的演示在 tests/e2e/test_learning_pipeline.py。
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agentruntime.registry.v1 import registry_pb2 as rg
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_learning.evals.schemas import problems, validator
from agentruntime_learning.pipeline.__main__ import main
from agentruntime_learning.pipeline.run import PipelineConfig, PipelineReport, run_pipeline

from pipeline_support import FAMILY, ROUTE_B, FakeRegistry, ScriptedGateway, console_lines, otlp_lines, population, route_task

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
GOOD = dict(a_success=115, a_fail=5, b_success=85, b_fail=35)
BAD = dict(a_success=100, a_fail=40, b_success=90, b_fail=2)


def config(tmp_path: Path, **kw: Any) -> PipelineConfig:
    return PipelineConfig(tenant_id="t1", task_family=FAMILY, report_dir=tmp_path / "reports", **kw)


async def run(tmp_path: Path, lines: list[str], *, registry: FakeRegistry | None = None, telemetry: ConsoleTelemetryProvider | None = None,
              gateway: ScriptedGateway | None = None, fmt: str = "console", **kw: Any) -> tuple[PipelineReport, FakeRegistry, ConsoleTelemetryProvider]:
    registry = registry or FakeRegistry()
    telemetry = telemetry or ConsoleTelemetryProvider(stream=io.StringIO())
    report = await run_pipeline(config(tmp_path, **kw), lines=lines, fmt=fmt, registry=registry, gateway=gateway, telemetry=telemetry, now=NOW)
    return report, registry, telemetry


# --- 未过门槛：发布被拒 ---


async def test_a_failing_candidate_is_never_written_to_the_registry(tmp_path: Path) -> None:
    report, registry, _ = await run(tmp_path, console_lines(population(**BAD)))
    assert report.status == "rejected"
    assert report.reasons and any("成功率" in r for r in report.reasons)
    assert registry.writes == [], "未过门槛时 Registry 的写方法一次都不调用"
    assert report.evidence is not None and report.evidence["passed"] is False


async def test_too_few_samples_are_rejected(tmp_path: Path) -> None:
    report, registry, _ = await run(tmp_path, console_lines(population(a_success=20, a_fail=1, b_success=5)))
    assert report.status == "rejected" and any("样本" in r for r in report.reasons)
    assert registry.writes == []


async def test_no_candidate_means_no_write(tmp_path: Path) -> None:
    report, registry, _ = await run(tmp_path, console_lines(population(a_success=3, a_fail=1)))
    assert report.status == "no_candidate"
    assert registry.writes == []


# --- 过门槛：发布成功 ---


async def test_a_passing_candidate_is_published_then_promoted(tmp_path: Path) -> None:
    report, registry, _ = await run(tmp_path, console_lines(population(**GOOD)))
    assert report.status == "published", report.reasons
    assert [c[0] for c in registry.writes] == ["publish", "promote"], "先影子发布，再带证据晋升"
    publish, promote = registry.writes[0][1], registry.writes[1][1]
    assert publish["tenant"] == "t1", "从租户的 trace 学来的 Recipe 发布在该租户的作用域"
    assert publish["kind"] == rg.REGISTRY_KIND_RECIPE and publish["name"] == "learned_finance_invoice_coding"
    assert problems(validator("recipe"), publish["document"]) == []
    assert promote["version"] == "v1" and promote["tenant"] == "t1"
    assert promote["evaluation"] == report.evidence
    assert problems(validator("registry_evaluation"), promote["evaluation"]) == []
    assert report.recipe == "learned_finance_invoice_coding@v1"


async def test_the_existing_default_recipe_is_the_baseline(tmp_path: Path) -> None:
    registry = FakeRegistry()
    ref = registry.promote_default("t1", "invoice_std", {"task_family": FAMILY})
    specs = [*population(**GOOD), *[route_task(500 + i, ROUTE_B, recipe=ref, status="done" if i % 4 else "aborted") for i in range(120)]]
    report, _, _ = await run(tmp_path, console_lines(specs), registry=registry)
    assert report.baseline == {"kind": "recipe", "recipe": ref, "samples": 120}


async def test_a_registry_refusal_is_reported(tmp_path: Path) -> None:
    registry = FakeRegistry()
    registry.reject_promote = True
    report, _, _ = await run(tmp_path, console_lines(population(**GOOD)), registry=registry)
    assert report.status == "promote_failed"
    assert [c[0] for c in registry.writes] == ["publish", "promote"]
    assert all(r.state != "default" for r in registry.recipes), "默认版本不变，影子版本留在 Registry 里"


async def test_dry_run_never_writes(tmp_path: Path) -> None:
    report, registry, _ = await run(tmp_path, console_lines(population(**GOOD)), dry_run=True)
    assert report.status == "dry_run" and report.evidence["passed"] is True
    assert registry.writes == []


# --- 租户 ---


async def test_other_tenants_do_not_change_anything(tmp_path: Path) -> None:
    mine = console_lines(population(**GOOD))
    alone, _, _ = await run(tmp_path / "a", mine)
    noisy = mine + console_lines(population(**BAD, tenant="t2", salt="t2")) + console_lines(population(a_success=300, a_fail=0, tenant="t3", salt="t3"))
    mixed, _, _ = await run(tmp_path / "b", noisy)
    assert mixed.status == alone.status == "published"
    assert mixed.candidate_document == alone.candidate_document
    assert {k: v for k, v in mixed.evidence.items() if k != "report"} == {k: v for k, v in alone.evidence.items() if k != "report"}
    assert mixed.other_tenant_spans > 0


async def test_each_read_is_audited_on_the_platform_scope(tmp_path: Path) -> None:
    lines = console_lines(population(**GOOD)) + console_lines(population(a_success=5, a_fail=0, tenant="t2", salt="t2"))
    _, _, telemetry = await run(tmp_path, lines)
    [audit] = [entry for entry in telemetry.exported_logs if entry.message == "learning.read_traces"]
    assert audit.attributes["scope"] == "platform" and "tenant_id" not in audit.attributes
    assert audit.attributes["learning.tenant"] == "t1"
    assert audit.attributes["learning.other_tenant_spans"] > 0
    assert audit.attributes["learning.tasks"] == 240


# --- 预算与指标 ---


async def test_learning_spend_is_its_own_metric(tmp_path: Path) -> None:
    gateway = ScriptedGateway([], cost_micros=2000)
    report, _, telemetry = await run(tmp_path, console_lines(population(**GOOD)), gateway=gateway, budget_usd=0.5)
    assert report.budget["calls"] == len(gateway.requests) > 0
    costs = [m for m in telemetry.exported_metrics if m.name == "learning.cost_usd"]
    assert costs and all(m.attributes["scope"] == "platform" for m in costs)
    assert sum(m.value for m in costs) == pytest.approx(report.budget["spent_usd"])
    assert not [m for m in telemetry.exported_metrics if m.name.startswith("task.")], "不写 Task 的指标"


async def test_failures_are_attributed_in_the_report(tmp_path: Path) -> None:
    gateway = ScriptedGateway([{"category": "verification", "reason": "缺 judge"}] * 200)
    report, _, _ = await run(tmp_path, console_lines(population(**GOOD)), gateway=gateway, budget_usd=1)
    assert report.attributions, "失败的 Task 有归因"
    assert set(report.attribution_summary) <= {"specification", "coordination", "verification", "unattributed"}


# --- 报告与确定性 ---


async def test_the_report_is_written_and_named_in_the_evidence(tmp_path: Path) -> None:
    report, _, _ = await run(tmp_path, console_lines(population(**GOOD)))
    assert report.path is not None and report.path.parent == tmp_path / "reports"
    saved = json.loads(report.path.read_text("utf-8"))
    assert saved["status"] == "published" and saved["evidence"] == report.evidence
    assert report.evidence["report"] == report.path.name
    assert report.evidence["evaluated_at"] == "2026-10-23T09:00:00Z"


async def test_same_traces_same_result(tmp_path: Path) -> None:
    lines = console_lines(population(**GOOD))
    first, _, _ = await run(tmp_path / "1", lines)
    second, _, _ = await run(tmp_path / "2", list(reversed(lines)))
    assert first.candidate_document == second.candidate_document
    assert first.evidence == second.evidence


async def test_otlp_json_input_gives_the_same_result(tmp_path: Path) -> None:
    specs = population(**GOOD)
    console, _, _ = await run(tmp_path / "c", console_lines(specs))
    otlp, _, _ = await run(tmp_path / "o", otlp_lines(specs), fmt="otlp_json")
    assert otlp.candidate_document == console.candidate_document and otlp.evidence == console.evidence


# --- 命令行 ---


def test_cli_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    traces = tmp_path / "spans.jsonl"
    traces.write_text("\n".join(console_lines(population(**GOOD))), "utf-8")
    code = main(["run", "--tenant", "t1", "--family", FAMILY, "--traces", str(traces), "--format", "console",
                 "--report-dir", str(tmp_path / "r"), "--dry-run"], env={})
    assert code == 0
    out = capsys.readouterr().out
    assert "dry_run" in out and "learned_finance_invoice_coding" in out


def test_cli_needs_the_registry_unless_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    traces = tmp_path / "spans.jsonl"
    traces.write_text("\n".join(console_lines(population(**GOOD))), "utf-8")
    code = main(["run", "--tenant", "t1", "--family", FAMILY, "--traces", str(traces), "--report-dir", str(tmp_path / "r")], env={})
    assert code == 2
    assert "AR_REGISTRY_ADDRESS" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--family", FAMILY, "--traces", "x"],
        ["run", "--tenant", "t1", "--traces", "x"],
        ["run", "--tenant", "t1", "--family", "not-a-family", "--traces", "x"],
        ["run", "--tenant", "T1!", "--family", FAMILY, "--traces", "x"],
        ["run", "--tenant", "t1", "--family", FAMILY, "--traces", "x", "--budget-usd", "-1"],
    ],
)
def test_cli_rejects_bad_arguments(argv: list[str], tmp_path: Path) -> None:
    assert main([*argv, "--report-dir", str(tmp_path), "--dry-run"], env={}) == 2


def test_cli_missing_trace_file(tmp_path: Path) -> None:
    code = main(["run", "--tenant", "t1", "--family", FAMILY, "--traces", str(tmp_path / "nope.jsonl"), "--report-dir", str(tmp_path), "--dry-run"], env={})
    assert code == 2
