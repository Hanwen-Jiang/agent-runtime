"""D-12：跨后端的配额、预算与有序回退（docs/plan/cloud-deploy.md D-12 的验收；设计书改动稿 W10、W11）。

1. 契约（租户策略 schema 的正例与反例）在 tests/contract；这里核对：配额超了是 sandbox_quota_exceeded、花费超了是
   sandbox_budget_exceeded，都是 denied。
2. 并发：同时建 20 个环境、配额 5，恰好成功 5 个（SQLite 与 Postgres 各一遍）。
3. 回退：首选后端 transient 时落到下一个；denied、invalid 不回退；回退列表里只剩更弱的后端时拒绝；绑定 workspace 时只在
   共用工作区的后端之间回退；配置里没登记的回退后端、写错的隔离强度一律拒绝。
4. 环境表与 span 里记着实际落点与回退原因。
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from agentruntime_core.adapters import pgtest
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.errors import DeniedError, ErrorCategory, InvalidError, TransientError
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.broker import SandboxBroker
from agentruntime_worker.sandbox.ledger import SandboxLedger
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.tools.sandboxes import Sandboxes

TA = TenantContext(tenant_id="t-a", user_id="u1", workspace_id="w1")
SPECS: dict[str, dict[str, Any]] = {
    "gvisor": {"kind": "local", "isolation": "container"},
    "kata": {"kind": "local", "isolation": "microvm"},
    "cloudflare": {"kind": "local", "isolation": "microvm"},
    "plain": {"kind": "local", "isolation": "none"},
}


@pytest.fixture(scope="module")
def pg_cluster():
    try:
        cluster = pgtest.Cluster()
    except pgtest.MissingTool as err:
        if pgtest.allow_missing():
            pytest.skip(str(err))
        pytest.fail(str(err), pytrace=False)
    yield cluster
    cluster.stop()


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Any]:
    if request.param == "sqlite":
        provider = SQLiteRelationalStoreProvider(tmp_path / "relational")
        yield provider
        await provider.aclose()
        return
    from agentruntime_core.adapters.postgres import PostgresRelationalStoreProvider

    cluster = request.getfixturevalue("pg_cluster")
    db = cluster.new_database("d12")
    provider = await PostgresRelationalStoreProvider.connect(cluster.dsn(pgtest.APP_ROLE, db), max_conns=8)
    yield provider
    await provider.aclose()
    cluster.drop_database(db)


def make(tmp_path: Path, store: Any, policy: Mapping[str, Any], specs: Mapping[str, Mapping[str, Any]] | None = None, default: str = "gvisor") -> SandboxBroker:
    workspaces = tmp_path / "workspaces"
    (workspaces / "t-a" / "w1").mkdir(parents=True, exist_ok=True)

    async def lookup(tenant_id: str) -> Mapping[str, Any] | None:
        return policy if tenant_id == "t-a" else None

    boxes = Sandboxes(dict(specs or SPECS), default, workspace_root=workspaces, policy=lookup)
    return SandboxBroker(boxes, SandboxLedger(store))


async def fail_next_create(broker: SandboxBroker, backend: str, category: ErrorCategory) -> None:
    provider = (await broker.sandboxes.backend(backend)).provider
    provider._faults.fail_next("create", category)  # type: ignore[attr-defined]


async def test_twenty_concurrent_creates_with_quota_five_give_exactly_five(tmp_path: Path, store: Any) -> None:
    broker = make(tmp_path, store, {"sandbox": {"backend": "gvisor", "quota": {"max_concurrent": 5}}})
    results = await asyncio.gather(*(broker.acquire(TA, SandboxSpec()) for _ in range(20)), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, BaseException)]
    denied = [r for r in results if isinstance(r, DeniedError)]
    assert len(ok) == 5 and len(denied) == 15, [type(r).__name__ for r in results]
    assert {e.code for e in denied} == {"sandbox_quota_exceeded"}
    assert len(await broker.ledger.active("t-a")) == 5  # type: ignore[union-attr]
    await broker.release(ok[0])
    again = await broker.acquire(TA, SandboxSpec())
    assert again.record is not None, "销毁一个之后又能建"


async def test_daily_and_monthly_budgets_are_denied_as_budget_exceeded(tmp_path: Path, store: Any) -> None:
    broker = make(tmp_path, store, {"sandbox": {"backend": "gvisor", "quota": {"daily_cost_usd": 1.0}}})
    ledger = broker.ledger
    assert ledger is not None
    await ledger.add_usage("t-a", ledger.clock(), cost_usd=1.5)
    with pytest.raises(DeniedError) as info:
        await broker.acquire(TA, SandboxSpec())
    assert info.value.code == "sandbox_budget_exceeded"
    monthly = make(tmp_path / "m", store, {"sandbox": {"backend": "gvisor", "quota": {"monthly_cost_usd": 1.0}}})
    with pytest.raises(DeniedError) as info:
        await monthly.acquire(TA, SandboxSpec())
    assert info.value.code == "sandbox_budget_exceeded", "当月各天加起来"


async def test_daily_sandbox_seconds_count_running_environments(tmp_path: Path, store: Any) -> None:
    broker = make(tmp_path, store, {"sandbox": {"backend": "gvisor", "quota": {"daily_sandbox_seconds": 60}}})
    ledger = broker.ledger
    assert ledger is not None
    await ledger.add_usage("t-a", ledger.clock(), sandbox_seconds=61)
    with pytest.raises(DeniedError) as info:
        await broker.acquire(TA, SandboxSpec())
    assert info.value.code == "sandbox_quota_exceeded"


async def test_transient_on_the_preferred_backend_falls_back_and_is_recorded(tmp_path: Path) -> None:
    broker = make(tmp_path, SQLiteRelationalStoreProvider(tmp_path / "relational"), {"sandbox": {"backend": "kata", "fallback": ["gvisor", "cloudflare"]}})
    await fail_next_create(broker, "kata", ErrorCategory.TRANSIENT)
    lease = await broker.acquire(TA, SandboxSpec())
    assert lease.backend.name == "cloudflare", "gvisor 比 kata 弱，跳过；落到同为 microvm 的 cloudflare"
    assert lease.record is not None and lease.record.fallback_from == "kata"
    assert lease.fallback_reason.startswith("kata：")
    failed = [r for r in await _all(broker, "t-a") if r.state == "failed"]
    assert len(failed) == 1 and failed[0].backend == "kata" and failed[0].reason


async def test_denied_and_invalid_are_not_retried_elsewhere(tmp_path: Path) -> None:
    broker = make(tmp_path, SQLiteRelationalStoreProvider(tmp_path / "relational"), {"sandbox": {"backend": "kata", "fallback": ["cloudflare"]}})
    for category, error in ((ErrorCategory.DENIED, DeniedError), (ErrorCategory.INVALID, InvalidError)):
        await fail_next_create(broker, "kata", category)
        with pytest.raises(error):
            await broker.acquire(TA, SandboxSpec())
    assert not [r for r in await _all(broker, "t-a") if r.backend == "cloudflare"], "没有回退到 cloudflare"


async def test_only_weaker_backends_left_is_refused(tmp_path: Path) -> None:
    broker = make(tmp_path, SQLiteRelationalStoreProvider(tmp_path / "relational"), {"sandbox": {"backend": "kata", "fallback": ["gvisor", "plain"]}})
    await fail_next_create(broker, "kata", ErrorCategory.TRANSIENT)
    with pytest.raises(TransientError) as info:
        await broker.acquire(TA, SandboxSpec())
    assert "gvisor" in info.value.message and "plain" in info.value.message, "说明更弱的后端不用"
    assert {r.backend for r in await _all(broker, "t-a")} == {"kata"}, "绝不降级"


async def test_explicit_min_isolation_can_raise_the_floor(tmp_path: Path) -> None:
    broker = make(tmp_path, SQLiteRelationalStoreProvider(tmp_path / "relational"),
                  {"sandbox": {"backend": "gvisor", "fallback": ["kata"], "min_isolation": "microvm"}})
    lease = await broker.acquire(TA, SandboxSpec())
    assert lease.backend.name == "kata", "首选本身低于 min_isolation 时直接用回退里够强的"


async def test_workspace_bound_fallback_stays_on_the_same_volume(tmp_path: Path) -> None:
    specs = {
        "gvisor": {"kind": "k8s", "isolation": "container", "workspace_claim": "workspaces"},
        "kata": {"kind": "k8s", "isolation": "microvm", "workspace_claim": "workspaces"},
        "cloudflare": {"kind": "cloudflare", "isolation": "microvm", "bridge_url": "https://bridge.example.workers.dev"},
    }
    broker = make(tmp_path, SQLiteRelationalStoreProvider(tmp_path / "relational"), {"sandbox": {"backend": "gvisor", "fallback": ["cloudflare", "kata"]}}, specs)
    candidates, skipped = broker._candidates(await broker.sandboxes.policy_for("t-a"), SandboxSpec(workspace_id="w1"))
    assert candidates == ["gvisor", "kata"] and any("cloudflare" in s and "工作区" in s for s in skipped)
    candidates, _ = broker._candidates(await broker.sandboxes.policy_for("t-a"), SandboxSpec())
    assert candidates == ["gvisor", "cloudflare", "kata"], "不绑工作区时照常回退"


async def test_policy_errors_are_refused_not_ignored(tmp_path: Path) -> None:
    for policy, code in (
        ({"sandbox": {"backend": "gvisor", "fallback": ["firecracker-x"]}}, "unknown_sandbox_backend"),
        ({"sandbox": {"backend": "gvisor", "min_isolation": "vm"}}, "invalid_min_isolation"),
    ):
        broker = make(tmp_path, SQLiteRelationalStoreProvider(tmp_path / "relational"), policy)
        with pytest.raises(InvalidError) as info:
            await broker.acquire(TA, SandboxSpec())
        assert info.value.code == code


async def test_fallback_shows_in_the_shell_run_span(tmp_path: Path) -> None:
    broker = make(tmp_path, SQLiteRelationalStoreProvider(tmp_path / "relational"), {"sandbox": {"backend": "kata", "fallback": ["cloudflare"]}})
    await fail_next_create(broker, "kata", ErrorCategory.TRANSIENT)
    telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
    tools = LocalToolDriverProvider()
    register_platform_tools(tools, PlatformToolsConfig(workspace_root=tmp_path / "workspaces", shell_sandbox="sandbox"), sandboxes=broker, telemetry=telemetry)
    out = (await tools.bind(TA).invoke("shell_run", "v1", {"argv": ["python", "-c", "print(1)"]}, idempotency_key="idk-fb")).output
    assert out["sandbox"] == "cloudflare"
    await telemetry.bind(TA).flush()
    [span] = [s for s in telemetry.exported_spans if s.name == "runtime.sandbox"]
    assert span.attributes["sandbox.fallback_from"] == "kata" and span.attributes["sandbox.fallback_reason"].startswith("kata：")


async def _all(broker: SandboxBroker, owner: str) -> list[Any]:
    """这个租户的全部行（含已结束的）。"""
    ledger = broker.ledger
    assert ledger is not None
    async with ledger.bind(owner).transaction(read_only=True) as tx:
        rows = await tx.query("SELECT env_ref FROM sandbox_envs ORDER BY created_at")
    return [await ledger.get(owner, r["env_ref"]) for r in rows]


def test_quota_parses_numbers_from_the_policy() -> None:
    from agentruntime_worker.sandbox.policy import SandboxPolicy

    policy = SandboxPolicy.from_document({"sandbox": {"backend": "kata", "idle_timeout_s": 7200, "max_lifetime_s": 600,
                                                      "quota": {"max_concurrent": 3, "daily_cost_usd": 0, "monthly_cost_usd": 2}}}, "gvisor")
    assert policy.quota.max_concurrent == 3 and policy.quota.monthly_cost_usd == 2.0 and policy.quota.daily_cost_usd == 0.0
    assert policy.idle_timeout == timedelta(minutes=15), "只能调低部署的默认值"
    assert policy.max_lifetime == timedelta(minutes=10)
    assert SandboxPolicy.from_document(None, "gvisor").backend == "gvisor"
