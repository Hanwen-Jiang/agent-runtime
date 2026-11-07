"""Direct evaluations must be attributable without exposing their prompts."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_learning.evals.audited_gateway import AuditedGateway, task_identity
from agentruntime_learning.evals.budget import BudgetError


def request(*, cap=1000, tenant="evals"):
    req = mg.GenerateRequest()
    req.tenant.tenant_id = tenant
    req.call.max_cost_usd_micros = cap
    req.call.task_id = "shared-old-eval-id"
    req.call.node_id = "plan"
    req.generation.messages.add(role=mg.CHAT_ROLE_USER, text="private prompt never in journal")
    return req


class Provider:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    async def generate(self, req):
        self.calls.append(req)
        if self.failure:
            raise self.failure
        out = mg.GenerateResponse(text="private response never in journal")
        out.usage.cost_usd_micros = 123
        out.model.model_profile_id = "test/profile@v1"
        return out


async def test_run_identity_and_per_call_sequence_survive_restart(tmp_path):
    inner = Provider()
    gateway = AuditedGateway(inner, tmp_path, "mx-fixed-run")
    await gateway.generate(request())
    gateway.close()
    gateway = AuditedGateway(inner, tmp_path, "mx-fixed-run")
    await gateway.generate(request())
    gateway.close()
    assert {r.call.task_id for r in inner.calls} == {task_identity("mx-fixed-run")}
    assert [r.call.node_id for r in inner.calls] == ["eval_call_00000001", "eval_call_00000002"]
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [row["state"] for row in rows] == ["pending", "completed", "pending", "completed"]
    assert "private prompt" not in (tmp_path / "calls.jsonl").read_text()
    assert "private response" not in (tmp_path / "calls.jsonl").read_text()


async def test_interrupted_call_is_not_reissued_on_resume(tmp_path):
    inner = Provider(TimeoutError("ambiguous"))
    gateway = AuditedGateway(inner, tmp_path, "deepeval-fixed-run")
    with pytest.raises(TimeoutError):
        await gateway.generate(request())
    gateway.close()
    inner.failure = None
    resumed = AuditedGateway(inner, tmp_path, "deepeval-fixed-run")
    with pytest.raises(BudgetError, match="reconcile"):
        await resumed.generate(request())
    resumed.close()
    assert len(inner.calls) == 1


@pytest.mark.parametrize("req", [request(cap=0), request(tenant="other")])
async def test_unbounded_or_cross_tenant_request_never_reaches_provider(tmp_path, req):
    inner = Provider()
    gateway = AuditedGateway(inner, tmp_path, "bounded-run")
    with pytest.raises(BudgetError):
        await gateway.generate(req)
    gateway.close()
    assert not inner.calls and not (tmp_path / "calls.jsonl").exists()


def test_same_directory_cannot_be_relabelled_or_owned_twice(tmp_path):
    gateway = AuditedGateway(Provider(), tmp_path, "run-one")
    with pytest.raises(ValueError, match="active owner"):
        AuditedGateway(Provider(), tmp_path, "run-one")
    gateway.close()
    with pytest.raises(ValueError, match="identity changed"):
        AuditedGateway(Provider(), tmp_path, "run-two")


async def test_explicit_isolated_budget_tenant_keeps_source_identity(tmp_path):
    inner = Provider()
    gateway = AuditedGateway(inner, tmp_path, "mx-isolated", tenant="evalmx20261106")
    original = request()
    await gateway.generate(original)
    gateway.close()
    assert original.tenant.tenant_id == "evals"
    assert inner.calls[0].tenant.tenant_id == "evalmx20261106"
    identity = json.loads((tmp_path / "identity.json").read_text())
    assert identity["source_tenant_id"] == "evals" and identity["tenant_id"] == "evalmx20261106"


async def test_explicit_offset_preserves_task_identity_and_never_reuses_prior_rpc_nodes(tmp_path):
    inner = Provider()
    gateway = AuditedGateway(inner, tmp_path, "fixed-run", sequence_offset=1)
    assert gateway.sequence == 1
    await gateway.generate(request())
    gateway.close()
    original_identity = (tmp_path / "identity.json").read_bytes()
    resumed = AuditedGateway(inner, tmp_path, "fixed-run", sequence_offset=1)
    assert resumed.sequence == 2
    await resumed.generate(request())
    resumed.close()
    assert [r.call.node_id for r in inner.calls] == ["eval_call_00000002", "eval_call_00000003"]
    assert {r.call.task_id for r in inner.calls} == {task_identity("fixed-run")}
    assert json.loads(original_identity)["sequence_offset"] == 1
    assert (tmp_path / "identity.json").read_bytes() == original_identity


def test_zero_offset_keeps_existing_identity_byte_compatible(tmp_path):
    gateway = AuditedGateway(Provider(), tmp_path, "legacy-run")
    gateway.close()
    original = (tmp_path / "identity.json").read_bytes()
    assert "sequence_offset" not in json.loads(original)
    resumed = AuditedGateway(Provider(), tmp_path, "legacy-run", sequence_offset=0)
    assert resumed.sequence == 0
    resumed.close()
    assert (tmp_path / "identity.json").read_bytes() == original


@pytest.mark.parametrize("offset", [-1, True, 1.0, "1", None])
def test_invalid_offset_is_rejected_before_creating_artifacts(tmp_path, offset):
    out = tmp_path / "invalid"
    with pytest.raises(ValueError, match="nonnegative integer"):
        AuditedGateway(Provider(), out, "fixed-run", sequence_offset=offset)
    assert not out.exists()


@pytest.mark.parametrize("offset", [0, 2])
def test_existing_offset_identity_cannot_be_changed(tmp_path, offset):
    gateway = AuditedGateway(Provider(), tmp_path, "fixed-run", sequence_offset=1)
    gateway.close()
    before = (tmp_path / "identity.json").read_bytes()
    with pytest.raises(ValueError, match="identity changed"):
        AuditedGateway(Provider(), tmp_path, "fixed-run", sequence_offset=offset)
    assert (tmp_path / "identity.json").read_bytes() == before


@pytest.mark.parametrize("sequence", [0, 1, -1, True, 1.5, "2", None])
def test_loaded_journal_cannot_include_any_sequence_at_or_below_offset(tmp_path, sequence):
    gateway = AuditedGateway(Provider(), tmp_path, "fixed-run", sequence_offset=1)
    gateway.close()
    rows = [{"sequence": sequence, "state": "completed"}, {"sequence": 2, "state": "completed"}]
    original = "".join(json.dumps(row) + "\n" for row in rows)
    (tmp_path / "calls.jsonl").write_text(original)
    with pytest.raises(ValueError, match="above the frozen offset"):
        AuditedGateway(Provider(), tmp_path, "fixed-run", sequence_offset=1)
    assert (tmp_path / "calls.jsonl").read_text() == original


async def test_offset_never_waives_an_unresolved_call_in_its_own_journal(tmp_path):
    inner = Provider(TimeoutError("ambiguous"))
    gateway = AuditedGateway(inner, tmp_path, "fixed-run", sequence_offset=1)
    with pytest.raises(TimeoutError):
        await gateway.generate(request())
    gateway.close()
    before = (tmp_path / "calls.jsonl").read_bytes()
    inner.failure = None
    resumed = AuditedGateway(inner, tmp_path, "fixed-run", sequence_offset=1)
    with pytest.raises(BudgetError, match="reconcile"):
        await resumed.generate(request())
    resumed.close()
    assert len(inner.calls) == 1 and inner.calls[0].call.node_id == "eval_call_00000002"
    assert (tmp_path / "calls.jsonl").read_bytes() == before


async def test_authoritative_ledger_preserves_pending_time_and_token_fields(tmp_path, monkeypatch):
    from agentruntime_core.adapters.postgres import PostgresRelationalStoreProvider
    from agentruntime_learning.evals.audited_gateway import ledger

    gateway = AuditedGateway(Provider(), tmp_path, "fixed-run", tenant="isolated", sequence_offset=1)
    gateway.close()
    old = {"reservation_id": "old-pending", "task_id": task_identity("fixed-run"), "node_id": "eval_call_00000001",
           "model_profile_id": "judge", "method": "generate", "actual_usd_micros": None,
           "reserved_usd_micros": 298752, "reserved_at": 1794080000123, "input_tokens": None,
           "output_tokens": None, "registry_scope": "platform", "registry_revision": "v3",
           "registry_document_sha256": "a" * 64}
    class Store:
        closed = False
        def bind(self, context):
            assert context.tenant_id == "isolated"
            return self
        @asynccontextmanager
        async def transaction(self, *, read_only):
            assert read_only is True
            yield self
        async def query(self, sql, args):
            selected = sql.split("FROM", 1)[0]
            for field in ("reserved_at", "input_tokens", "output_tokens"):
                assert field in selected
            assert args == [task_identity("fixed-run")]
            return [dict(old)]
        async def aclose(self):
            self.closed = True
    store = Store()
    async def connect(dsn):
        assert dsn == "test-only"
        return store
    monkeypatch.setattr(PostgresRelationalStoreProvider, "connect", connect)
    monkeypatch.setenv("AR_RELATIONAL_DSN", "test-only")
    result = await ledger(tmp_path)
    assert result["attempts"] == [old], "the offset must not hide the old pending reservation"
    assert result["pending_reserved_usd_micros"] == 298752 and result["cost_known"] is False
    assert store.closed
