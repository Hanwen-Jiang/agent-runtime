"""ENV-01：只在实际健康确认和同会话复借边界写事实，普通命令成功不冒充它们。"""

from datetime import UTC, datetime, timedelta

from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.ledger import SandboxLedger, iso


async def test_health_confirmation_and_reuse_are_explicit_ledger_facts(tmp_path):
    store = SQLiteRelationalStoreProvider(tmp_path)
    now = datetime(2026, 11, 3, tzinfo=UTC)
    ledger = SandboxLedger(store, clock=lambda: now)
    try:
        creating = await ledger.begin_create("t1", backend="test", kind="k8s", isolation="microvm", workspace_id="w1", session_id="ses-a", idle=timedelta(minutes=5), max_lifetime=timedelta(hours=1))
        record = await ledger.mark_ready(creating, "pod-a")
        assert record.health_confirmed_at == "" and record.reused_at == "", "state=ready alone is not health confirmation"
        record = await ledger.touch(record, cpu_ms=12, task_id="task-1")
        assert not record.health_confirmed_at and not record.reused_at, "successful command/touch cannot imply health or reuse"
        now += timedelta(seconds=1)
        record = await ledger.touch(record, health_confirmed=True, reused=True)
        assert record.health_confirmed_at == record.reused_at == iso(now)
        assert record.expires_at == iso(now + timedelta(minutes=5))
        # A later failed health observation clears confirmation, with no invented lifecycle transition.
        record = await ledger.touch(record, health_confirmed=False)
        assert not record.health_confirmed_at and record.state == "ready"
        assert (await ledger.get("t1", record.env_ref)) == record
        assert await ledger.get("t2", record.env_ref) is None
    finally:
        await store.aclose()


async def test_broker_records_health_and_real_session_workspace_reuse(tmp_path, monkeypatch):
    from agentruntime_core.dev.sandbox import LocalSubprocessSandbox
    from agentruntime_core.providers.sandbox import SandboxEnv, SandboxSpec
    from agentruntime_worker.sandbox.broker import SandboxBroker
    from test_sandbox_sessions import Setup, S1

    original = LocalSubprocessSandbox.create
    health_calls = []

    async def create(self, spec):
        env = await original(self, spec)
        return SandboxEnv(env.env_id, spec, health_confirmed=True)

    async def healthy(self, env_id):
        self._env(env_id)
        health_calls.append(env_id)
        return True

    monkeypatch.setattr(LocalSubprocessSandbox, "create", create)
    monkeypatch.setattr(LocalSubprocessSandbox, "healthy", healthy, raising=False)
    s = Setup(tmp_path)
    tenant = TenantContext(tenant_id="t1", workspace_id="w1", session_id=S1)
    spec = SandboxSpec(workspace_id="w1")
    try:
        first = await s.broker.acquire(tenant, spec, reuse=True)
        assert first.record.health_confirmed_at and not first.record.reused_at
        s.clock.now += timedelta(seconds=1)
        # A new broker reads the same authoritative ledger, not process-local reuse flags.
        other = SandboxBroker(s.broker.sandboxes, s.ledger)
        second = await other.acquire(tenant, spec, reuse=True)
        assert second.record.env_ref == first.record.env_ref
        assert second.record.reused_at == second.record.health_confirmed_at == iso(s.clock.now)
        assert health_calls == [first.env_id]
    finally:
        await s.ledger._store.aclose()
