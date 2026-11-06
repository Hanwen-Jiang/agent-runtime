"""Direct evaluations must be attributable without exposing their prompts."""
from __future__ import annotations

import json

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
