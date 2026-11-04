"""R-06-D：Learning 的成本读业务库的 model_usage（代审第 7 节第 1、5 条，docs/plan/learning-pipeline.md 第 8 节）。

Model Gateway 每次 provider attempt 预留时写一行、结算时填实际金额（libs/go-core/modelbudget，迁移 301、302）；Learning 读同一事实源，
不再信 trace 上的 runtime.cost_usd。守住的：

- 每个 Task 的成本是它全部尝试的实际金额之和（失败的尝试也算）；有还没结算的尝试，或账本里没有这个 Task 的记录，算成本未知
  （不按 0 算，门槛交人工判断）。没有 task_id 的调用（例如 Learning 自己的失败归因）不算进任何 Task。
- 只读这个租户的行：租户由服务端绑定（SQLite 是租户自己的库文件，Postgres 是行级安全），SQL 里没有租户条件。
- 读不了账本（表不存在、库连不上）时整次运行失败（退出码 4），不改消费组的位置，不退回 trace 上的成本。
- `--cost-source` 默认 auto：配了业务库（AR_RELATIONAL_DSN 或 AR_DATA_DIR）读账本，否则照旧读 trace；报告写明用的是哪个。
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.providers.relational_store import Migration
from agentruntime_core.tenancy import TenantContext
from agentruntime_learning.pipeline.__main__ import main
from agentruntime_learning.pipeline.costs import CostSourceError, TaskCost, apply_costs, ledger_costs
from agentruntime_learning.pipeline.run import PipelineConfig, run_pipeline
from agentruntime_learning.pipeline.traces import read_spans, task_traces

from pipeline_support import FAMILY, FakeRegistry, console_lines, population

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
GOOD = dict(a_success=115, a_fail=5, b_success=85, b_fail=35)

# 与 Go 的 modelbudget.Migrations() 301、302 同样的表（Python 一侧只读，不拥有这张表的迁移）。
LEDGER = Migration(version=301, name="model_usage_test", statements=(
    """CREATE TABLE model_usage (reservation_id TEXT PRIMARY KEY, reserved_at BIGINT NOT NULL, reserved_usd_micros BIGINT NOT NULL,
       actual_usd_micros BIGINT, task_id TEXT NOT NULL, node_id TEXT NOT NULL, model_profile_id TEXT NOT NULL, vendor TEXT NOT NULL,
       method TEXT NOT NULL, price_currency TEXT, price_unit TEXT, price_at TEXT, in_usd_micros_per_m BIGINT, out_usd_micros_per_m BIGINT,
       cache_read_usd_micros_per_m BIGINT, input_tokens BIGINT, output_tokens BIGINT, cache_read_tokens BIGINT)""",
))


async def seed(directory: Path, tenant: str, rows: list[tuple[str, int, int | None]]) -> SQLiteRelationalStoreProvider:
    """rows：(task_id, 预留, 实际或 None 表示还没结算)。"""
    provider = SQLiteRelationalStoreProvider(directory)
    await provider.migrate([LEDGER])
    async with provider.bind(TenantContext(tenant_id=tenant)).transaction() as tx:
        n = len(await tx.query("SELECT reservation_id FROM model_usage"))
        for i, (task, reserved, actual) in enumerate(rows, start=n):
            await tx.execute(
                "INSERT INTO model_usage (reservation_id, reserved_at, reserved_usd_micros, actual_usd_micros, task_id, node_id, model_profile_id, vendor, method)"
                " VALUES ($1, $2, $3, $4, $5, 'n', 'p@1', 'v', 'generate')",
                (f"mbr_{tenant}_{i}", 1_762_243_200_000 + i, reserved, actual, task),
            )
    return provider


async def test_each_task_costs_the_sum_of_its_attempts_and_pending_or_missing_is_unknown(tmp_path: Path) -> None:
    provider = await seed(tmp_path, "t1", [
        ("task-a", 2_000_000, 1_200_000), ("task-a", 900_000, 300_000),  # 两次尝试都结算了（一次失败也计费）
        ("task-b", 500_000, 400_000), ("task-b", 700_000, None),         # 一次还没结算
        ("", 50_000, 50_000),                                            # 没有 task_id 的调用不归任何 Task
    ])
    costs = await ledger_costs(provider, "t1", ["task-a", "task-b", "task-c"])
    assert costs["task-a"] == TaskCost(usd=1.5, attempts=2, pending=0) and costs["task-a"].known
    assert costs["task-b"] == TaskCost(usd=0.4, attempts=2, pending=1) and not costs["task-b"].known, "有一次没结算：成本未知"
    assert costs["task-c"] == TaskCost(usd=0.0, attempts=0, pending=0) and not costs["task-c"].known, "账本里没有：成本未知，不是 0"
    await provider.aclose()


async def test_only_the_bound_tenants_rows_count(tmp_path: Path) -> None:
    provider = await seed(tmp_path, "t1", [("task-a", 1_000_000, 1_000_000)])
    await seed(tmp_path, "t2", [("task-a", 9_000_000, 9_000_000)])
    assert (await ledger_costs(provider, "t1", ["task-a"]))["task-a"].usd == 1.0
    assert (await ledger_costs(provider, "t2", ["task-a"]))["task-a"].usd == 9.0
    await provider.aclose()


async def test_many_tasks_are_read_in_chunks(tmp_path: Path) -> None:
    rows: list[tuple[str, int, int | None]] = [(f"task-{i}", 10, 10) for i in range(1200)]
    provider = await seed(tmp_path, "t1", rows)
    costs = await ledger_costs(provider, "t1", [f"task-{i}" for i in range(1200)])
    assert len(costs) == 1200 and all(c.known and c.usd == 0.00001 for c in costs.values())
    await provider.aclose()


async def test_a_missing_ledger_table_is_an_error_not_free(tmp_path: Path) -> None:
    provider = SQLiteRelationalStoreProvider(tmp_path)
    with pytest.raises(CostSourceError) as raised:
        await ledger_costs(provider, "t1", ["task-a"])
    assert "model_usage" in str(raised.value)
    await provider.aclose()


def test_ledger_costs_replace_the_trace_costs() -> None:
    specs = population(a_success=2, a_fail=1)
    tasks = task_traces(read_spans(console_lines(specs), fmt="console"), "t1").tasks
    first, second, third = (t.task_id for t in tasks)
    out = apply_costs(tasks, {first: TaskCost(usd=2.5, attempts=3, pending=0), second: TaskCost(usd=0.1, attempts=1, pending=1)})
    by_id = {t.task_id: t for t in out}
    assert by_id[first].cost_usd == 2.5 and by_id[first].cost_known
    assert by_id[second].cost_usd == 0.1 and not by_id[second].cost_known
    assert by_id[third].cost_usd == 0.0 and not by_id[third].cost_known, "账本里没有的 Task 成本未知"
    assert [t.task_id for t in out] == [t.task_id for t in tasks], "顺序不变"


def config(tmp_path: Path, **kw: Any) -> PipelineConfig:
    return PipelineConfig(tenant_id="t1", task_family=FAMILY, report_dir=tmp_path / "reports", **kw)


async def run(tmp_path: Path, costs: Any) -> Any:
    registry = FakeRegistry()
    report = await run_pipeline(config(tmp_path), lines=console_lines(population(**GOOD)), fmt="console", registry=registry, gateway=None,
                                telemetry=ConsoleTelemetryProvider(stream=io.StringIO()), now=NOW, costs=costs)
    return report, registry


async def test_the_gate_uses_the_ledger_and_the_report_says_so(tmp_path: Path) -> None:
    async def known(tenant_id: str, task_ids: list[str]) -> dict[str, TaskCost]:
        assert tenant_id == "t1"
        return {t: TaskCost(usd=0.1, attempts=1, pending=0) for t in task_ids}

    report, registry = await run(tmp_path, known)
    assert report.cost_source == "model_usage" and report.status == "published", report.reasons
    assert report.cost_unknown == 0
    assert json.loads(report.path.read_text("utf-8"))["cost_source"] == "model_usage"


async def test_one_unsettled_attempt_sends_the_comparison_to_a_person(tmp_path: Path) -> None:
    async def one_pending(tenant_id: str, task_ids: list[str]) -> dict[str, TaskCost]:
        out = {t: TaskCost(usd=0.1, attempts=1, pending=0) for t in task_ids}
        out[sorted(task_ids)[0]] = TaskCost(usd=0.1, attempts=2, pending=1)
        return out

    report, registry = await run(tmp_path, one_pending)
    assert report.status == "rejected" and registry.writes == [], "成本未知不自动晋级"
    assert report.cost_unknown == 1
    assert any("人工" in r for r in report.reasons)


async def test_without_a_ledger_the_trace_costs_are_used_and_named(tmp_path: Path) -> None:
    report, _ = await run(tmp_path, None)
    assert report.cost_source == "trace" and report.status == "published"


async def test_a_ledger_read_failure_stops_the_run(tmp_path: Path) -> None:
    async def broken(tenant_id: str, task_ids: list[str]) -> dict[str, TaskCost]:
        raise CostSourceError("读不了业务库的 model_usage")

    with pytest.raises(CostSourceError):
        await run(tmp_path, broken)


# --- 命令行 ---


def cli(tmp_path: Path, env: dict[str, str], *extra: str) -> int:
    traces = tmp_path / "traces.jsonl"
    traces.write_text("\n".join(console_lines(population(**GOOD))), "utf-8")
    return main(["run", "--tenant", "t1", "--family", FAMILY, "--traces", str(traces), "--format", "console",
                 "--report-dir", str(tmp_path / "reports"), "--budget-usd", "0", "--dry-run", *extra], env=env)


def last_summary(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out.strip().splitlines()
    return json.loads(out[-1])


def report_of(summary: dict[str, Any]) -> dict[str, Any]:
    return json.loads(Path(summary["report"]).read_text("utf-8"))


def test_cli_auto_reads_the_ledger_when_the_business_store_is_configured(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    specs = population(**GOOD)
    tasks = task_traces(read_spans(console_lines(specs), fmt="console"), "t1").tasks

    async def prepare() -> None:
        provider = await seed(tmp_path / "data" / "relational", "t1", [(t.task_id, 200_000, 100_000) for t in tasks])
        await provider.aclose()

    asyncio.run(prepare())
    assert cli(tmp_path, {"AR_DATA_DIR": str(tmp_path / "data")}) == 0
    report = report_of(last_summary(capsys))
    assert report["cost_source"] == "model_usage" and report["cost_unknown"] == 0


def test_cli_auto_without_a_store_reads_the_trace(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(tmp_path, {}) == 0
    assert report_of(last_summary(capsys))["cost_source"] == "trace"


def test_cli_trace_can_be_chosen_explicitly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(tmp_path, {"AR_DATA_DIR": str(tmp_path / "data")}, "--cost-source", "trace") == 0
    assert report_of(last_summary(capsys))["cost_source"] == "trace"


def test_cli_model_usage_without_a_store_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(tmp_path, {}, "--cost-source", "model_usage") == 2
    assert "AR_RELATIONAL_DSN" in capsys.readouterr().err


def test_cli_exits_4_when_the_ledger_cannot_be_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "data" / "relational").mkdir(parents=True)
    assert cli(tmp_path, {"AR_DATA_DIR": str(tmp_path / "data")}) == 4
    err = capsys.readouterr().err
    assert "model_usage" in err and str(tmp_path) not in err, "错误里不带库的位置"
