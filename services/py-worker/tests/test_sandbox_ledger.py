"""D-11：沙箱环境表与对账（docs/plan/cloud-deploy.md D-11 的验收；docs/plan/sandbox-platform.md）。

1. 每个后端（本地、Cloudflare 假桥；K8s 设 AR_TEST_KIND=1）建、用、销毁都在环境表里留下 creating → ready → destroying →
   destroyed；存活时间、花费（按后端的价目）记在行里，并计入当天的用量。
2. 故障：在“记了 creating、后端已建好”之间中断，对账后行记为 failed、后端里的孤儿被删掉；在“记了 destroying、后端还没删”
   之间中断，对账再删；后端手工删掉的环境记为丢失；刚建、还没来得及落账的环境在宽限内不删；空闲到期的会话环境被回收，
   命令还在跑的不回收。
3. 越权：租户 A 的环境在租户 B 的环境表里查不到（SQLite 按库文件、Postgres 按行级安全，两种都跑）。
4. 一次 shell_run 的花费、环境号与 Task 记在 span 与环境表里。
5. 对账工作流：Worker 启动时确保它在跑（固定 ID，重复启动沿用同一个），每隔 interval_s 跑一轮 activity。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from agentruntime_core.adapters import cloudflaretest, pgtest
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker import names
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.sandbox.broker import SandboxBroker
from agentruntime_worker.sandbox.ledger import EnvRecord, SandboxLedger, iso
from agentruntime_worker.sandbox.reconcile import ReconcileActivities, reconcile_once
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.tools.sandboxes import Sandboxes
from agentruntime_worker.worker import ensure_sandbox_reconcile
from agentruntime_worker.workflows.sandbox_reconcile import ReconcileInput, SandboxReconcileWorkflow

SESSION = "ses_01HZY2J5K3M4N5P6Q7R8S9T0VW"
TA = TenantContext(tenant_id="t-a", user_id="u1", workspace_id="w1", session_id=SESSION)
TB = TenantContext(tenant_id="t-b", user_id="u2", workspace_id="w1")
GRACE = timedelta(minutes=10)


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class RecordingLedger(SandboxLedger):
    """记下每次目录写入的（环境，状态），用来核对状态序列。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.transitions: list[tuple[str, str]] = []

    async def _directory(self, record: Any, state: str) -> None:
        self.transitions.append((record.env_ref, state))
        await super()._directory(record, state)

    def states(self, env_ref: str) -> list[str]:
        return [s for r, s in self.transitions if r == env_ref]


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
    db = cluster.new_database("d11")
    provider = await PostgresRelationalStoreProvider.connect(cluster.dsn(pgtest.APP_ROLE, db))
    yield provider
    await provider.aclose()
    cluster.drop_database(db)


def policy_from(policies: Mapping[str, Mapping[str, Any]]):
    async def lookup(tenant_id: str) -> Mapping[str, Any] | None:
        return policies.get(tenant_id)

    return lookup


def make(tmp_path: Path, store: Any, clock: Clock, specs: Mapping[str, Mapping[str, Any]] | None = None, *, default: str = "local",
         secrets_file: Path | None = None, policies: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[SandboxBroker, RecordingLedger]:
    workspaces = tmp_path / "workspaces"
    (workspaces / "t-a" / "w1").mkdir(parents=True, exist_ok=True)
    (workspaces / "t-b" / "w1").mkdir(parents=True, exist_ok=True)
    boxes = Sandboxes(
        specs or {"local": {"kind": "local", "isolation": "none", "price": {"per_hour": 3.6, "per_cpu_hour": 36}}},
        default, workspace_root=workspaces, secrets_file=secrets_file, policy=policy_from(policies or {}),
    )
    ledger = RecordingLedger(store, clock=clock)
    return SandboxBroker(boxes, ledger), ledger


async def lifecycle(broker: SandboxBroker, ledger: RecordingLedger, clock: Clock, tenant: TenantContext, spec: SandboxSpec) -> EnvRecord:
    lease = await broker.acquire(tenant, spec, task_id="task-1")
    assert lease.record is not None and lease.record.state == "ready" and lease.record.backend_env_id == lease.env_id
    result = await lease.box.exec(lease.env_id, ["python", "-c", "print(6 * 7)"], timeout=timedelta(seconds=30))
    assert result.stdout.strip() == b"42"
    clock.advance(timedelta(minutes=30))
    await broker.release(lease, result=result)
    assert lease.record.state == "destroyed"
    assert ledger.states(lease.record.env_ref) == ["creating", "ready", "destroying", "destroyed"]
    return lease.record


async def test_local_backend_lifecycle_is_recorded_with_cost_and_usage(tmp_path: Path, store: Any) -> None:
    clock = Clock()
    broker, ledger = make(tmp_path, store, clock)
    record = await lifecycle(broker, ledger, clock, TA, SandboxSpec(workspace_id="w1"))
    stored = await ledger.get("t-a", record.env_ref)
    assert stored is not None and stored.state == "destroyed" and stored.task_id == "task-1" and stored.workspace_id == "w1"
    assert 30 * 60_000 <= stored.alive_ms < 31 * 60_000
    assert stored.cost_usd == pytest.approx(stored.alive_ms / 3_600_000 * 3.6, rel=1e-3), "按存活时间与价目算花费"
    usage = await ledger.usage("t-a", clock.now.strftime("%Y-%m-%d"))
    assert usage.sandbox_seconds == stored.alive_ms // 1000 and usage.cost_usd == pytest.approx(stored.cost_usd)


@pytest.fixture(scope="module")
def bridge():
    try:
        b = cloudflaretest.FakeBridge()
    except cloudflaretest.MissingTool as err:
        pytest.fail(str(err), pytrace=False)
    yield b
    b.stop()


async def test_cloudflare_backend_lifecycle_is_recorded(tmp_path: Path, bridge: cloudflaretest.FakeBridge) -> None:
    secrets = tmp_path / "secrets.json"
    LocalFileSecretStoreProvider(secrets).seed_secret(None, "sandbox/cloudflare-bridge-token", bridge.token)
    clock = Clock()
    store = SQLiteRelationalStoreProvider(tmp_path / "relational")
    broker, ledger = make(tmp_path, store, clock, {"cf": {"kind": "cloudflare", "isolation": "microvm", "bridge_url": bridge.url}},
                          default="cf", secrets_file=secrets)
    record = await lifecycle(broker, ledger, clock, TA, SandboxSpec())
    assert record.backend == "cf" and record.isolation == "microvm" and record.backend_env_id.startswith("cfs-")
    listed = await (await broker.sandboxes.backend("cf")).provider.list_environments()  # type: ignore[attr-defined]
    assert record.backend_env_id not in {e.env_id for e in listed}, "销毁后桥的目录里也没了"


@pytest.mark.skipif(os.environ.get("AR_TEST_KIND") != "1", reason="设 AR_TEST_KIND=1 才用 kind 跑 K8s 后端")
async def test_k8s_backend_lifecycle_is_recorded(tmp_path: Path) -> None:
    from agentruntime_core.adapters import kindtest

    cluster = kindtest.Cluster()
    try:
        specs = {"k8s": {"kind": "k8s", "isolation": "container", "namespace": "sandboxes", "image": cluster.python_image,
                         "agent_image": cluster.agent_image, "connect": "api_proxy", "kubeconfig": str(cluster.kubeconfig)}}
        clock = Clock()
        broker, ledger = make(tmp_path, SQLiteRelationalStoreProvider(tmp_path / "relational"), clock, specs, default="k8s")
        record = await lifecycle(broker, ledger, clock, TA, SandboxSpec())
        assert record.backend_env_id.startswith("sbx-")
        await broker.sandboxes.aclose()
    finally:
        cluster.stop()


async def test_crash_after_backend_create_is_repaired_without_orphans(tmp_path: Path, store: Any) -> None:
    clock = Clock()
    broker, ledger = make(tmp_path, store, clock)

    async def killed(*args: Any, **kwargs: Any) -> EnvRecord:
        raise RuntimeError("Worker 在这里被杀掉")

    ledger.mark_ready = killed  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await broker.acquire(TA, SandboxSpec())
    [env_ref] = {r for r, _ in ledger.transitions}
    assert (await ledger.get("t-a", env_ref)).state == "creating"  # type: ignore[union-attr]
    local = (await broker.sandboxes.backend("local")).provider
    assert len(await local.list_environments()) == 1, "后端里已经建好了"  # type: ignore[attr-defined]
    report = await reconcile_once(broker, grace=GRACE)
    assert report.orphans_destroyed == 0 and report.stale_failed == 0, "宽限之内什么都不动"
    clock.advance(GRACE + timedelta(minutes=1))
    report = await reconcile_once(broker, grace=GRACE)
    assert report.stale_failed == 1 and report.orphans_destroyed == 1, report
    assert (await ledger.get("t-a", env_ref)).state == "failed"  # type: ignore[union-attr]
    assert await local.list_environments() == []  # type: ignore[attr-defined]
    assert await ledger.directory() == [], "目录里不再有没结束的环境"


async def test_crash_between_destroying_and_backend_delete_is_retried(tmp_path: Path, store: Any) -> None:
    clock = Clock()
    broker, ledger = make(tmp_path, store, clock)
    lease = await broker.acquire(TA, SandboxSpec())
    assert lease.record is not None
    await ledger.begin_destroy(lease.record, "Worker 删到一半退出")
    report = await reconcile_once(broker, grace=GRACE)
    assert report.destroy_retried == 1, report
    assert (await ledger.get("t-a", lease.record.env_ref)).state == "destroyed"  # type: ignore[union-attr]
    assert await (await broker.sandboxes.backend("local")).provider.list_environments() == []  # type: ignore[attr-defined]


async def test_environment_deleted_behind_our_back_is_marked_lost(tmp_path: Path, store: Any) -> None:
    clock = Clock()
    broker, ledger = make(tmp_path, store, clock)
    lease = await broker.acquire(TA, SandboxSpec())
    assert lease.record is not None
    await lease.box.destroy(lease.env_id)  # 有人直接在后端删掉了
    report = await reconcile_once(broker, grace=GRACE)
    assert report.lost == 1
    lost = await ledger.get("t-a", lease.record.env_ref)
    assert lost is not None and lost.state == "lost" and "列表里没有" in lost.reason


async def test_fresh_unrecorded_environment_is_left_alone_until_the_grace_passes(tmp_path: Path, store: Any) -> None:
    clock = Clock()
    broker, ledger = make(tmp_path, store, clock)
    local = (await broker.sandboxes.backend("local")).provider
    env = await local.bind(TA).create(SandboxSpec())  # 绕过 broker：环境表里没有它
    assert (await reconcile_once(broker, grace=GRACE)).orphans_destroyed == 0
    assert [e.env_id for e in await local.list_environments()] == [env.env_id]  # type: ignore[attr-defined]
    clock.advance(GRACE + timedelta(seconds=1))
    assert (await reconcile_once(broker, grace=GRACE)).orphans_destroyed == 1
    assert await local.list_environments() == []  # type: ignore[attr-defined]


async def test_idle_session_environment_is_reaped_but_a_busy_one_is_not(tmp_path: Path, store: Any) -> None:
    clock = Clock()
    broker, ledger = make(tmp_path, store, clock)
    idle = await broker.acquire(TA, SandboxSpec(workspace_id="w1", session_id=SESSION), reuse=True)
    await broker.release(idle, keep=True)
    other = TenantContext(tenant_id="t-a", user_id="u1", workspace_id="w2", session_id=SESSION)
    (tmp_path / "workspaces" / "t-a" / "w2").mkdir(parents=True)
    busy = await broker.acquire(other, SandboxSpec(workspace_id="w2", session_id=SESSION), reuse=True, busy_for=timedelta(hours=1))
    clock.advance(timedelta(minutes=16))
    report = await reconcile_once(broker, grace=GRACE)
    assert report.reaped == 1, report
    assert (await ledger.get("t-a", idle.record.env_ref)).state == "destroyed"  # type: ignore[union-attr]
    assert (await ledger.get("t-a", busy.record.env_ref)).state == "ready", "命令还在跑的环境不回收"  # type: ignore[union-attr]


async def test_other_tenants_cannot_see_the_environment(tmp_path: Path, store: Any) -> None:
    clock = Clock()
    broker, ledger = make(tmp_path, store, clock)
    lease = await broker.acquire(TA, SandboxSpec())
    assert lease.record is not None
    assert await ledger.get("t-b", lease.record.env_ref) is None
    assert await ledger.active("t-b") == []
    assert await ledger.by_backend_env("t-b", None, lease.env_id) is None
    assert [r.env_ref for r in await ledger.active("t-a")] == [lease.record.env_ref]
    await broker.release(lease)


async def test_tenant_deletion_destroys_every_environment(tmp_path: Path, store: Any) -> None:
    clock = Clock()
    broker, ledger = make(tmp_path, store, clock)
    leases = [await broker.acquire(TA, SandboxSpec()) for _ in range(3)]
    keep = await broker.acquire(TB, SandboxSpec())
    assert await broker.destroy_tenant("t-a") == 3
    assert await ledger.active("t-a") == []
    assert [r.env_ref for r in await ledger.active("t-b")] == [keep.record.env_ref]  # type: ignore[union-attr]
    remaining = await (await broker.sandboxes.backend("local")).provider.list_environments()  # type: ignore[attr-defined]
    assert {e.env_id for e in remaining} == {keep.env_id} and all(lease.env_id not in {e.env_id for e in remaining} for lease in leases)


async def test_shell_run_records_cost_env_and_task_in_the_span(tmp_path: Path) -> None:
    clock = Clock()
    broker, ledger = make(tmp_path, SQLiteRelationalStoreProvider(tmp_path / "relational"), clock)
    telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
    tools = LocalToolDriverProvider()
    register_platform_tools(tools, PlatformToolsConfig(workspace_root=tmp_path / "workspaces", shell_sandbox="sandbox"), sandboxes=broker, telemetry=telemetry)
    tenant = TenantContext(tenant_id="t-a", user_id="u1", workspace_id="w1")
    out = (await tools.bind(tenant).invoke("shell_run", "v1", {"argv": ["python", "-c", "print(1)"]}, idempotency_key="idk-cost", task_id="task-77")).output
    assert out["exit_code"] == 0 and out["sandbox"] == "local"
    await telemetry.bind(tenant).flush()
    [span] = [s for s in telemetry.exported_spans if s.name == "runtime.sandbox"]
    env_ref = span.attributes["sandbox.env_ref"]
    record = await ledger.get("t-a", env_ref)
    assert record is not None and record.state == "destroyed" and record.task_id == "task-77"
    assert span.attributes["sandbox.cost_usd"] == pytest.approx(record.cost_usd)
    assert span.attributes["sandbox.backend"] == "local" and span.attributes["sandbox.reused"] is False


@pytest.mark.temporal
async def test_reconcile_workflow_runs_rounds_and_starts_once(temporal_env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    client: Client = temporal_env.client
    calls: list[ReconcileInput] = []

    class Counting(ReconcileActivities):
        @activity.defn(name=names.RECONCILE_SANDBOXES)
        async def reconcile_sandboxes(self, inp: ReconcileInput):  # type: ignore[no-untyped-def,override]
            calls.append(inp)
            return await super().reconcile_sandboxes(inp)

    clock = Clock()
    broker, _ = make(tmp_path, SQLiteRelationalStoreProvider(tmp_path / "relational"), clock)
    activity_fn = Counting(broker).activities()[names.RECONCILE_SANDBOXES]
    config = WorkerConfig(sandbox_reconcile_interval_s=1)
    suffix = os.urandom(4).hex()
    workflow_id, wf_queue, act_queue = f"test-sandbox-reconcile-{suffix}", f"test-reconcile-wf-{suffix}", f"test-reconcile-act-{suffix}"
    queues = {"workflow_id": workflow_id, "task_queue": wf_queue, "activity_queue": act_queue}
    async with (
        Worker(client, task_queue=wf_queue, workflows=[SandboxReconcileWorkflow]),
        Worker(client, task_queue=act_queue, activities=[activity_fn]),
    ):
        await ensure_sandbox_reconcile(client, config, **queues)
        await ensure_sandbox_reconcile(client, config, **queues)  # 几个副本同时启动：沿用同一个
        for _ in range(300):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.1)
        handle = client.get_workflow_handle(workflow_id)
        runs = [w async for w in client.list_workflows(f"WorkflowId = '{workflow_id}'")]
        await handle.terminate("测试结束")
    assert len(calls) >= 2 and calls[0].interval_s == 1 and calls[0].activity_queue == act_queue
    assert len({r.run_id for r in runs}) == 1, "两次启动只有一个运行"


def test_iso_times_sort_lexically() -> None:
    a, b = datetime(2026, 10, 27, 9, 5, tzinfo=UTC), datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
    assert iso(a) < iso(b) and json.dumps(iso(a))
