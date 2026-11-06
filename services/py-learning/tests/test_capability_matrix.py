"""能力矩阵的调度（capability.matrix）：计划、钉模型、落盘与续跑、花费、淘汰赛、追加样本、实时指标。"""

from __future__ import annotations

import json
import asyncio
from pathlib import Path
from typing import Any

import pytest

import capability_support as cs
from agentruntime_learning.capability import matrix as mx
from agentruntime_learning.capability import scoring as sc
from agentruntime_learning.capability.base import role_enum
from agentruntime_learning.capability.evals import load_eval, resolve

ASTRA = "a6api/gpt-6-astra@2026-11"
FABLE = "a6api/claude-fable-5-1@2026-11"


@pytest.fixture(autouse=True)
def _fresh_evals() -> None:
    cs.reset()


def plan(**overrides: Any) -> mx.MatrixPlan:
    cell = {"role": "plan", "tier": "medium", "evals": [{"eval": "capability_support:make", "options": overrides.pop("options", {})}], "candidates": overrides.pop("candidates", [cs.SONNET, cs.OPUS])}
    cell.update(overrides.pop("cell", {}))
    data = {"name": "t1", "n": 40, "r": 3, "seed": 0, "concurrency": 3, "cells": [cell], **overrides}
    return mx.parse_plan(data)


def synthetic() -> cs.SyntheticEval:
    return cs.make()


# --- 加载与计划 ---


def test_short_eval_names_resolve_inside_the_package() -> None:
    assert resolve("s09") == ("agentruntime_learning.capability.evals.s09", "make")
    assert resolve("s09:route") == ("agentruntime_learning.capability.evals.s09", "route")
    assert resolve("capability_support:make") == ("agentruntime_learning.capability.evals.capability_support", "make")
    assert resolve("pkg.mod:f") == ("pkg.mod", "f")
    with pytest.raises(ValueError):
        resolve("s09:")
    with pytest.raises(ValueError, match="找不到"):
        load_eval("no_such_eval")
    with pytest.raises(ValueError, match="不是 CellEval"):
        load_eval("json:dumps", {"obj": 1})
    assert isinstance(load_eval("capability_support:make"), cs.SyntheticEval), "本包里没有的短名再当顶层模块找"


def test_plans_are_checked() -> None:
    with pytest.raises(mx.PlanError, match="name"):
        mx.parse_plan({"cells": []})
    with pytest.raises(mx.PlanError, match="Tier 名"):
        plan(cell={"tier": "strong@medium"})
    with pytest.raises(mx.PlanError, match="expensive"):
        plan(cell={"expensive": [ASTRA]})
    with pytest.raises(mx.PlanError, match="重复"):
        mx.parse_plan({"name": "x", "cells": [{"role": "plan", "tier": "medium", "evals": ["s09"], "candidates": [cs.SONNET]}] * 2})
    with pytest.raises(mx.PlanError, match="role"):
        plan(cell={"role": "planner"})


def test_candidates_must_have_the_cells_tier() -> None:
    profiles = mx.load_profiles()
    mx.check_profiles(plan(candidates=[cs.SONNET, ASTRA, FABLE]), profiles)
    with pytest.raises(mx.PlanError, match="没有 Tier medium"):
        mx.check_profiles(plan(candidates=[cs.MINI]), profiles)
    with pytest.raises(mx.PlanError, match="不在 Profile 里"):
        mx.check_profiles(plan(candidates=["a6api/nope@1"]), profiles)
    with pytest.raises(mx.PlanError, match="嵌入"):
        mx.check_profiles(plan(cell={"tier": "default"}, candidates=["a6api/text-embedding-3-large@2026-10"]), profiles)


def test_the_new_expensive_profiles_come_from_models_dev() -> None:
    profiles = mx.load_profiles()
    for pid, released, cost in ((ASTRA, "2026-09-04", (10.0, 50.0)), (FABLE, "2026-09-01", (10.0, 50.0))):
        p = profiles[pid]
        assert p["class"] == "strong" and [t["name"] for t in p["tiers"]] == ["medium", "high"]
        assert p["released_at"] == released and (p["cost"]["in_per_m"], p["cost"]["out_per_m"]) == cost


# --- 跑 ---


async def test_every_run_gets_a_fresh_pin_on_the_role_and_tier_under_test(tmp_path: Path) -> None:
    gateway = cs.ServingGateway()
    runner = mx.MatrixRunner(plan(candidates=[cs.SONNET, cs.OPUS], r=2, n=10), gateway, tmp_path)
    report = await runner.run()
    rows = mx.read_results(tmp_path)
    assert len(rows) == 2 * 10 * 2 and runner.new_runs == 40
    assert {r.call.pinned_model_profile for r in gateway.requests} == {cs.SONNET, cs.OPUS}
    assert {r.call.tier for r in gateway.requests} == {"strong@medium"}
    row = rows[0]
    assert set(row) >= {"candidate", "item", "repeat", "score", "contaminated", "cost_usd", "record", "version", "pinned_calls"}
    assert row["record"]["served"] == row["candidate"] and row["pinned_calls"] == 1 and not row["contaminated"]
    assert (tmp_path / mx.SCORES).exists() and report["cells"][0]["role"] == "plan"
    assert json.loads((tmp_path / mx.PLAN_COPY).read_text("utf-8"))["name"] == "t1"


async def test_the_pinned_role_moves_to_the_candidates_class(tmp_path: Path) -> None:
    """压缩默认 small@low；mid 档的候选（deepseek-v4-flash）要改成 mid@low 才钉得上。"""
    gateway = cs.ServingGateway()
    p = plan(cell={"role": "transform", "tier": "low"}, candidates=[cs.MINI, cs.FLASH], options={"role": "transform"}, n=2, r=1)
    await mx.MatrixRunner(p, gateway, tmp_path).run()
    tiers = {r.call.pinned_model_profile: r.call.tier for r in gateway.requests}
    assert tiers == {cs.MINI: "small@low", cs.FLASH: "mid@low"}


async def test_a_switched_or_failed_pin_is_contaminated_and_not_scored(tmp_path: Path) -> None:
    gateway = cs.ServingGateway(switch_pins={cs.OPUS: cs.SONNET}, fail_pins=(cs.GPT55,))
    await mx.MatrixRunner(plan(candidates=[cs.SONNET, cs.OPUS, cs.GPT55], n=40, r=1), gateway, tmp_path).run()
    rows = mx.read_results(tmp_path)
    by = {c: [r for r in rows if r["candidate"] == c] for c in (cs.SONNET, cs.OPUS, cs.GPT55)}
    assert not any(r["contaminated"] for r in by[cs.SONNET])
    assert all(r["contaminated"] and r["missed_calls"] == 1 for r in by[cs.OPUS])
    assert all(r["contaminated"] and r["error"].startswith("RuntimeError") for r in by[cs.GPT55])
    scores = {c["candidate"]: c for c in json.loads((tmp_path / mx.SCORES).read_text("utf-8"))["cells"][0]["candidates"]}
    assert scores[cs.OPUS]["status"] == sc.CONTAMINATED and not scores[cs.OPUS]["publishable"]


async def test_a_second_run_resumes_and_skips_what_is_done(tmp_path: Path) -> None:
    p = plan(n=10, r=2)
    await mx.MatrixRunner(p, cs.ServingGateway(), tmp_path).run()
    lines = (tmp_path / mx.RESULTS).read_text("utf-8").splitlines()
    (tmp_path / mx.RESULTS).write_text("\n".join(lines[:-5]) + "\n" + lines[-5][:20], "utf-8")  # 打断：少了几行，最后一行写了一半
    gateway = cs.ServingGateway()
    runner = mx.MatrixRunner(p, gateway, tmp_path)
    await runner.run()
    assert runner.new_runs == 5 and len(gateway.requests) == 5
    assert len(mx.read_results(tmp_path)) == 40
    again = cs.ServingGateway()
    await mx.MatrixRunner(p, again, tmp_path).run()
    assert again.requests == []


async def test_resuming_with_a_changed_eval_set_is_refused(tmp_path: Path) -> None:
    await mx.MatrixRunner(plan(n=2, r=1), cs.ServingGateway(), tmp_path).run()
    synthetic().version = "v2"
    with pytest.raises(mx.ResumeMismatch, match="换一个输出目录"):
        mx.MatrixRunner(plan(n=2, r=1), cs.ServingGateway(), tmp_path)


async def test_item_and_total_spending_limits(tmp_path: Path) -> None:
    class CapAwareGateway(cs.ServingGateway):
        async def generate(self, request):
            response = await super().generate(request)
            if request.call.max_cost_usd_micros:
                response.usage.cost_usd_micros = min(response.usage.cost_usd_micros, request.call.max_cost_usd_micros)
            return response

    provider = CapAwareGateway(cost_micros=1000)
    p = plan(n=10, r=1, candidates=[cs.SONNET], item_usd_limit=0.0005, options={"uses_judge": True})
    await mx.MatrixRunner(p, provider, tmp_path / "item").run()
    rows = mx.read_results(tmp_path / "item")
    assert rows and all(r["error"] == "item_usd_limit" and r["contaminated"] for r in rows), "首次调用限定为500微美元，后续裁判调用在本地拒掉"
    assert all(r.call.max_cost_usd_micros == 500 for r in provider.requests)
    assert all(r["cost_usd"] == 0.0005 for r in rows)
    cs.reset()
    provider = CapAwareGateway(cost_micros=1000)
    runner = mx.MatrixRunner(plan(n=10, r=1, candidates=[cs.SONNET], concurrency=1), provider, tmp_path / "total", budget_usd=0.0035)
    await runner.run()
    assert runner.new_runs == 4 and runner.skipped_for_budget == 6 and runner.spent == pytest.approx(0.0035)
    assert [r.call.max_cost_usd_micros for r in provider.requests] == [3500, 2500, 1500, 500]


async def test_unknown_attempt_billing_stops_further_spending(tmp_path: Path) -> None:
    class Unknown(cs.ServingGateway):
        async def generate(self, request):
            self.requests.append(request)
            raise TimeoutError("outcome unknown")

    provider = Unknown()
    runner = mx.MatrixRunner(plan(n=10, r=1, candidates=[cs.SONNET], concurrency=1), provider, tmp_path, budget_usd=1)
    report = await runner.run()
    assert len(provider.requests) == 1 and runner.skipped_for_budget == 9
    assert report["budget"]["pending"] is True
    assert mx.read_results(tmp_path)[0]["cost_known"] is False
    again = mx.MatrixRunner(plan(n=10, r=1, candidates=[cs.SONNET], concurrency=1), provider, tmp_path, budget_usd=1)
    await again.run()
    assert len(provider.requests) == 1, "resume must not turn unknown billing into free attempts"


async def test_queued_budget_denials_are_not_model_observations(tmp_path: Path) -> None:
    class Delayed(cs.ServingGateway):
        async def generate(self, request):
            await asyncio.sleep(0.005)
            return await super().generate(request)

    provider = Delayed(cost_micros=1000)
    p = plan(n=5, r=1, candidates=[cs.SONNET], concurrency=3)
    runner = mx.MatrixRunner(p, provider, tmp_path, budget_usd=0.001)
    report = await runner.run()
    assert len(provider.requests) == 1
    assert len(mx.read_results(tmp_path)) == 1 and len(runner.done) == 1
    assert runner.skipped_for_budget == 4 and len(report["not_run"]) == 4


@pytest.mark.parametrize("change", [{"seed": 99}, {"n": 8}, {"r": 2}, {"candidates": [cs.SONNET]}])
async def test_resuming_cannot_change_frozen_plan(tmp_path: Path, change: dict) -> None:
    await mx.MatrixRunner(plan(n=2, r=1), cs.ServingGateway(), tmp_path).run()
    with pytest.raises(mx.ResumeMismatch):
        mx.MatrixRunner(plan(**({"n": 2, "r": 1} | change)), cs.ServingGateway(), tmp_path)


async def test_score_cannot_trim_frozen_candidates(tmp_path: Path) -> None:
    original = plan(n=2, r=1)
    await mx.MatrixRunner(original, cs.ServingGateway(), tmp_path).run()
    edited = plan(n=2, r=1, candidates=[cs.SONNET])
    with pytest.raises(mx.ResumeMismatch, match="冻结计划"):
        mx.verify_run_lock(tmp_path, edited, mx.read_results(tmp_path))
    rows = mx.read_results(tmp_path)
    rows[0]["version"] = "made-up-version"
    with pytest.raises(mx.ResumeMismatch, match="冻结题集"):
        mx.verify_run_lock(tmp_path, original, rows)


def test_frozen_run_requires_matching_actual_registry_exports(tmp_path: Path) -> None:
    from test_deepeval_acceptance import frozen

    snapshot = frozen()
    snapshot["subject_models"] = [cs.SONNET]
    snapshot["registry_revisions"] = [{
        "scope": "platform", "kind": "model_profile", "name": cs.SONNET,
        "version": "v2", "document_sha256": "e" * 64,
    }]
    p = plan(n=1, r=1, candidates=[cs.SONNET])
    for exported in ({}, {cs.SONNET: {"scope": "platform", "version": "v1", "sha256": "e" * 64}},
                     {cs.SONNET: {"scope": "platform", "version": "v2", "sha256": "a" * 64}}):
        with pytest.raises(mx.PlanError, match="Registry"):
            mx.MatrixRunner(p, cs.ServingGateway(), tmp_path, frozen=snapshot, registry_revisions=exported)
    assert not (tmp_path / mx.RESULTS).exists(), "无来源或失配时不能开始调用"


async def test_judged_evals_log_every_judgment_pinned_to_opus(tmp_path: Path) -> None:
    gateway = cs.ServingGateway()
    await mx.MatrixRunner(plan(n=3, r=1, candidates=[cs.SONNET], options={"uses_judge": True}), gateway, tmp_path).run()
    judged = [json.loads(line) for line in (tmp_path / mx.JUDGE_LOG).read_text("utf-8").splitlines()]
    assert len(judged) == 3 and {j["judge_served"] for j in judged} == {cs.OPUS} and all(j["judge_pinned_ok"] for j in judged)
    assert {j["candidate"] for j in judged} == {cs.SONNET} and {j["case_id"] for j in judged} == {"q000", "q001", "q002"}
    judge_requests = [r for r in gateway.requests if r.call.role == role_enum("judge")]
    assert {r.call.pinned_model_profile for r in judge_requests} == {cs.OPUS}


async def test_a_judge_cell_cannot_also_use_a_fixed_judge(tmp_path: Path) -> None:
    p = plan(cell={"role": "judge"}, options={"role": "judge", "uses_judge": True}, n=2, r=1)
    with pytest.raises(mx.PlanError, match="judge"):
        await mx.MatrixRunner(p, cs.ServingGateway(), tmp_path).run()


async def test_an_eval_for_another_role_is_refused(tmp_path: Path) -> None:
    with pytest.raises(mx.PlanError, match="测的是 route"):
        await mx.MatrixRunner(plan(options={"role": "route"}, n=2, r=1), cs.ServingGateway(), tmp_path).run()


# --- 淘汰赛与追加样本 ---


async def test_expensive_candidates_play_a_tournament_first(tmp_path: Path) -> None:
    skill = {cs.SONNET: 0.9, ASTRA: 0.1, FABLE: 0.9}
    p = plan(n=40, r=3, candidates=[cs.SONNET, ASTRA, FABLE], cell={"expensive": [ASTRA, FABLE]}, options={"skill": skill})
    report = await mx.MatrixRunner(p, cs.ServingGateway(), tmp_path).run()
    rows = mx.read_results(tmp_path)
    count = {c: sum(1 for r in rows if r["candidate"] == c) for c in skill}
    assert count == {cs.SONNET: 120, ASTRA: 20, FABLE: 120}, "不领先的只跑 n = 20、r = 1；可能领先的补到 n = 40、r = 3"
    status = {c["candidate"]: c["status"] for c in report["cells"][0]["candidates"]}
    assert status[ASTRA] == sc.NOT_LEADING


async def test_close_candidates_get_more_items_up_to_the_cap(tmp_path: Path) -> None:
    skill = {cs.SONNET: 0.80, cs.OPUS: 0.79, cs.GPT55: 0.20}
    p = plan(n=40, r=1, extend=True, max_n=80, candidates=list(skill), options={"skill": skill})
    await mx.MatrixRunner(p, cs.ServingGateway(), tmp_path).run()
    rows = mx.read_results(tmp_path)
    items = {c: len({r["item"] for r in rows if r["candidate"] == c}) for c in skill}
    assert items == {cs.SONNET: 80, cs.OPUS: 80, cs.GPT55: 40}, "只给区间与领先者重叠的候选（连同领先者）加题，每次 20 道，到上限为止"


# --- 估算与实时指标 ---


def test_the_estimate_uses_prices_then_observed_costs(tmp_path: Path) -> None:
    profiles = mx.load_profiles()
    p = plan(n=10, r=3, candidates=[cs.SONNET, ASTRA], cell={"expensive": [ASTRA]})
    evals = mx.load_evals(p)
    est = mx.estimate(p, profiles, evals, [])
    sonnet = next(r for r in est.rows if r["candidate"] == cs.SONNET)
    astra = next(r for r in est.rows if r["candidate"] == ASTRA)
    assert sonnet["runs"] == 30 and sonnet["per_run_usd"] == pytest.approx((1000 * 2.0 + 100 * 10.0) / 1e6)
    assert astra["runs"] == 10 and astra["if_advancing_runs"] == 20 and astra["per_run_usd"] == pytest.approx((1000 * 10 + 100 * 50) / 1e6)
    assert "合计还要约" in est.summary() and "淘汰赛后若晋级" in est.summary()
    done = [{"task_family": "general.unclassified", "role": "plan", "tier": "medium", "eval": "synthetic", "candidate": cs.SONNET, "cost_usd": 0.5}]
    observed = next(r for r in mx.estimate(p, profiles, evals, done).rows if r["candidate"] == cs.SONNET)
    assert observed["per_run_usd"] == 0.5 and observed["basis"] == "已跑的平均" and observed["remaining_usd"] == pytest.approx(0.5 * 29)


async def test_live_metrics_go_out_per_candidate(tmp_path: Path) -> None:
    telemetry = cs.FakeTelemetry()
    await mx.MatrixRunner(plan(n=4, r=1), cs.ServingGateway(), tmp_path, telemetry=telemetry).run()
    suites = {e[3]["eval.suite"] for e in telemetry.events}
    assert suites == {"capability"}
    cases = [e for e in telemetry.events if e[1] == "runtime.eval.cases"]
    assert len(cases) == 8 and {e[3]["eval.model"] for e in cases} == {cs.SONNET, cs.OPUS}
    assert sum(e[2] for e in telemetry.events if e[1] == "runtime.eval.cases_planned") == 8
    assert sum(1 for e in telemetry.events if e[1] == "runtime.eval.finished") == 2
