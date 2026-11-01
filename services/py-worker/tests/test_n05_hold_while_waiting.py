"""docs/plan/feedback-2026-11-01.md N-05 第 3 条：等人时环境不丢。

线上日志（2026-11-01）：第 4 轮问人之后等了 35 分钟，第 5 轮“sandbox started in 2.8s”，依赖要重装。会话级环境空闲 15 分钟就被对账回收，
不管它服务的 Task 是不是还在等人。现在：Task 还没结束时顺延一个空闲期，从最后一次使用算起最多 hold_max，也不超过活满。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.providers.relational_store import Migration
from agentruntime_core.providers.sandbox import SandboxSpec

from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.sandbox.reconcile import reconcile_once

from .test_sandbox_ledger import GRACE, TA, Clock, make
from .test_session_transcript import TASKS_DDL


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteRelationalStoreProvider]:
    provider = SQLiteRelationalStoreProvider(tmp_path / "relational")
    await provider.migrate([Migration(1, "tasks_like_task_service", (TASKS_DDL,))])
    yield provider
    await provider.aclose()


async def task_row(store: SQLiteRelationalStoreProvider, task_id: str, *, outcome: str = "") -> None:
    from agentruntime_core.tenancy import TenantContext

    async with store.bind(TenantContext(tenant_id="t-a")).transaction() as tx:
        await tx.execute(
            "INSERT INTO tasks (task_id, session_id, session_key, client_request_id, status, signaled, last_event_id, created_at, outcome) "
            "VALUES ($1, $2, '', $3, 0, 1, 0, 0, $4)",
            (task_id, TA.session_id, task_id, outcome),
        )


async def session_env(tmp_path: Path, store: SQLiteRelationalStoreProvider, clock: Clock, task_id: str):
    broker, ledger = make(tmp_path, store, clock)
    lease = await broker.acquire(TA, SandboxSpec(workspace_id="w1", session_id=TA.session_id), task_id=task_id, reuse=True)
    await broker.release(lease, keep=True)
    assert lease.record is not None
    return broker, ledger, lease.record.env_ref


async def test_an_environment_is_kept_while_its_task_waits(tmp_path: Path, store: SQLiteRelationalStoreProvider) -> None:
    clock = Clock()
    await task_row(store, "tsk_waiting")
    broker, ledger, ref = await session_env(tmp_path, store, clock, "tsk_waiting")
    clock.advance(timedelta(minutes=35))
    report = await reconcile_once(broker, grace=GRACE)
    assert report.reaped == 0 and report.held == 1, report
    assert (await ledger.get("t-a", ref)).state == "ready", "Task 还在等人：环境留着，回来接着用"  # type: ignore[union-attr]


async def test_the_hold_has_a_ceiling(tmp_path: Path, store: SQLiteRelationalStoreProvider) -> None:
    clock = Clock()
    await task_row(store, "tsk_waiting")
    broker, ledger, ref = await session_env(tmp_path, store, clock, "tsk_waiting")
    for _ in range(9):
        clock.advance(timedelta(minutes=16))
        await reconcile_once(broker, grace=GRACE, hold_max=timedelta(hours=2))
    assert (await ledger.get("t-a", ref)).state == "destroyed", "从最后一次使用算起超过 2 小时就回收"  # type: ignore[union-attr]


async def test_a_finished_task_does_not_hold_its_environment(tmp_path: Path, store: SQLiteRelationalStoreProvider) -> None:
    clock = Clock()
    await task_row(store, "tsk_done", outcome='{"status":"TASK_STATUS_DONE"}')
    broker, ledger, ref = await session_env(tmp_path, store, clock, "tsk_done")
    clock.advance(timedelta(minutes=16))
    report = await reconcile_once(broker, grace=GRACE)
    assert report.reaped == 1 and report.held == 0
    assert (await ledger.get("t-a", ref)).state == "destroyed"  # type: ignore[union-attr]


async def test_without_the_task_table_environments_are_reaped_as_before(tmp_path: Path) -> None:
    """Worker 与 task-service 不用同一个库（表不在）：按原来的规则回收，对账不报错。"""
    plain = SQLiteRelationalStoreProvider(tmp_path / "plain")
    try:
        clock = Clock()
        broker, ledger, ref = await session_env(tmp_path, plain, clock, "tsk_unknown")
        clock.advance(timedelta(minutes=16))
        report = await reconcile_once(broker, grace=GRACE)
        assert report.reaped == 1 and not report.errors
    finally:
        await plain.aclose()


def test_the_hold_is_configurable() -> None:
    assert WorkerConfig.from_env({}).sandbox_hold_max_s == 7200
    assert WorkerConfig.from_env({"AR_SANDBOX_HOLD_MAX_S": "600"}).sandbox_hold_max_s == 600
    with pytest.raises(ValueError):
        WorkerConfig.from_env({"AR_SANDBOX_HOLD_MAX_S": "-1"})
