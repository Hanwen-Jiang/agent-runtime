"""Shared per-attempt spend protection: concurrency, crash ambiguity and ownership."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_learning.evals.budget import BudgetError, BudgetGateway


class Provider:
    def __init__(self):
        self.seen = []

    async def generate(self, request):
        self.seen.append(request.call.max_cost_usd_micros)
        result = mg.GenerateResponse(text="ok")
        result.usage.cost_usd_micros = min(1500, request.call.max_cost_usd_micros)
        return result

    async def embed(self, request):
        self.seen.append(request.call.max_cost_usd_micros)
        result = mg.EmbedResponse()
        result.usage.cost_usd_micros = min(1500, request.call.max_cost_usd_micros)
        return result


async def test_generate_embed_and_concurrent_calls_share_one_cap(tmp_path):
    provider = Provider()
    guard = BudgetGateway(provider, tmp_path / "state.json", 0.0035)
    await guard.generate(mg.GenerateRequest())
    await guard.embed(mg.EmbedRequest())
    results = await asyncio.gather(*(guard.generate(mg.GenerateRequest()) for _ in range(2)), return_exceptions=True)
    assert provider.seen == [3500, 2000, 500]
    assert guard.state["spent_usd_micros"] == 3500
    assert isinstance(results[-1], BudgetError) and results[-1].billed_cost_usd_micros == 0
    guard.close()
    resumed = BudgetGateway(provider, tmp_path / "state.json", 0.0035)
    with pytest.raises(BudgetError, match="exhausted"):
        await resumed.generate(mg.GenerateRequest())
    resumed.close()


@pytest.mark.parametrize("prior", [-1, float("inf"), float("nan")])
def test_invalid_prior_cost_is_rejected(tmp_path, prior):
    with pytest.raises(ValueError, match="prior"):
        BudgetGateway(Provider(), tmp_path / "state.json", 1, prior_cost_usd=prior)


def test_second_process_cannot_own_the_same_budget(tmp_path):
    path = tmp_path / "state.json"
    guard = BudgetGateway(Provider(), path, 1)
    code = "from pathlib import Path; from agentruntime_learning.evals.budget import BudgetGateway; BudgetGateway(None, Path(__import__('sys').argv[1]), 1)"
    result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True)
    assert result.returncode != 0 and "active owner" in result.stderr
    guard.close()
    resumed = BudgetGateway(Provider(), path, 1)
    resumed.close()


async def test_ambiguous_failure_stays_pending_across_restart(tmp_path):
    class Unknown(Provider):
        async def generate(self, request):
            raise TimeoutError("provider outcome unknown")

    path = tmp_path / "state.json"
    guard = BudgetGateway(Unknown(), path, 1)
    with pytest.raises(TimeoutError):
        await guard.generate(mg.GenerateRequest())
    guard.close()
    resumed = BudgetGateway(Provider(), path, 1)
    with pytest.raises(BudgetError, match="reconcile"):
        await resumed.generate(mg.GenerateRequest())
    assert json.loads(path.read_text())["pending"]
    resumed.close()


async def test_nested_item_denial_does_not_reserve_total_allowance(tmp_path):
    item = BudgetGateway(Provider(), tmp_path / "item.json", 0.0015)
    total = BudgetGateway(item, tmp_path / "total.json", 1)
    await total.generate(mg.GenerateRequest())
    with pytest.raises(BudgetError):
        await total.generate(mg.GenerateRequest())
    assert total.state["spent_usd_micros"] == 1500 and not total.state["pending"]
    total.close(); item.close()


def test_resuming_cannot_raise_cap_or_discard_recorded_spend(tmp_path):
    path = tmp_path / "state.json"
    guard = BudgetGateway(Provider(), path, 1)
    guard.close()
    with pytest.raises(ValueError, match="original cap"):
        BudgetGateway(Provider(), path, 2)
    with pytest.raises(ValueError, match="behind recorded"):
        BudgetGateway(Provider(), path, 1, prior_cost_usd=0.5)
