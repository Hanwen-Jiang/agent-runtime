"""EV-DE: prevent mixed versions, fabricated evidence and resumed overspending."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agentruntime_learning.benchmarks import swebench as sb
from agentruntime_learning.evals import deepeval_acceptance as accept
from agentruntime_learning.evals import deliverables as dv
from agentruntime_learning.evals.judging import RecordingJudgeGateway
from swebench_support import Script, settings
from test_deliverable_evidence import DlvRuntime, tool
from test_judging import FakeGateway, judge_request


def frozen() -> dict:
    return {"runtime_commit": "a" * 40, "coding_agent_commit": "b" * 40,
            "deployment_manifest_sha256": "c" * 64, "memory_revision": "memory-service/release-42",
            "memory_config_sha256": "d" * 64, "subject_models": ["example/model@1"], "sub_agents": "off",
            "registry_revisions": [{"scope": "platform", "kind": "model_profile", "name": "example/model@1",
                                    "version": "v2", "document_sha256": "e" * 64}]}


def collected(tmp_path: Path, *, freeze: bool = True):
    case = dv.load_cases()[0]
    rt = DlvRuntime({case.instance_id: Script(events=[tool("succeeded")], cost_usd=0.05)})
    dv.run(rt, [case], tmp_path, settings(), frozen=frozen() if freeze else None)
    return case, rt


def test_resume_requires_same_versions_dataset_limits_and_fixture(tmp_path: Path):
    case, rt = collected(tmp_path)
    n = len(rt.submits)
    assert dv.run(rt, [case], tmp_path, settings(), frozen=frozen()).submitted == 0
    for config in (dict(frozen(), sub_agents="rules"), dict(frozen(), memory_revision="changed")):
        with pytest.raises(sb.ResumeMismatch):
            dv.run(rt, [case], tmp_path, settings(), frozen=config)
    with pytest.raises(sb.ResumeMismatch):
        dv.run(rt, [replace(case, expected_output="new annotation")], tmp_path, settings(), frozen=frozen())
    with pytest.raises(sb.ResumeMismatch):
        dv.run(rt, [case], tmp_path, settings(budget_usd=999), frozen=frozen())
    assert len(rt.submits) == n


def test_legacy_records_cannot_be_upgraded_into_frozen_run(tmp_path: Path):
    case, rt = collected(tmp_path, freeze=False)
    path = tmp_path / "run.json"
    doc = json.loads(path.read_text()); doc.pop("identity")
    path.write_text(json.dumps(doc))
    with pytest.raises(sb.ResumeMismatch, match="legacy"):
        dv.run(rt, [case], tmp_path, settings(), frozen=frozen())


def test_old_or_mismatched_tool_evidence_is_not_fresh_acceptance(tmp_path: Path):
    case, _ = collected(tmp_path)
    path = tmp_path / "records" / f"{case.instance_id}.json"
    rec = json.loads(path.read_text())
    assert accept.evidence_status(rec) == "captured"
    rec["tool_evidence"]["task_id"] = "other-task"
    path.write_text(json.dumps(rec))
    with pytest.raises(ValueError, match="task-bound"):
        dv.score(tmp_path, gateway=object(), judge_budget_usd=1)
    assert not (tmp_path / "scores" / "judgments.jsonl").exists()


def test_provenance_does_not_accept_profile_format_as_registry_revision():
    p = frozen()
    p["registry_revisions"][0]["version"] = "1"
    with pytest.raises(ValueError, match="published vN"):
        accept.provenance(p)
    p = frozen(); p["token"] = "must-not-save"
    with pytest.raises(ValueError, match="exactly"):
        accept.provenance(p)


async def test_every_fallback_attempt_consumes_remaining_budget_and_resume_preserves_it(tmp_path: Path):
    inner = FakeGateway(serve_first=["other/model@1"])
    path = tmp_path / "budget-state.json"
    limited = accept.BudgetGateway(inner, path, 0.003)
    recorder = RecordingJudgeGateway(limited, tmp_path / "judgments.jsonl")
    await recorder.generate(judge_request())
    assert [r.call.max_cost_usd_micros for r in inner.seen] == [3000, 1500]
    limited.close()
    resumed = accept.BudgetGateway(inner, path, 0.003)
    with pytest.raises(accept.JudgeBudgetError, match="exhausted"):
        await resumed.generate(judge_request())
    resumed.close()
    with pytest.raises(ValueError, match="original cap"):
        accept.BudgetGateway(inner, path, 2)
    assert len(inner.seen) == 2


async def test_concurrent_calls_do_not_share_an_unreserved_allowance(tmp_path: Path):
    inner = FakeGateway()
    limited = accept.BudgetGateway(inner, tmp_path / "state.json", 0.003)
    results = await asyncio.gather(*(limited.generate(judge_request()) for _ in range(3)), return_exceptions=True)
    assert len(inner.seen) == 2
    assert sum(isinstance(r, accept.JudgeBudgetError) for r in results) == 1
    limited.close()


async def test_nested_item_budget_rejection_does_not_leave_total_cost_pending(tmp_path: Path):
    inner = FakeGateway()
    item = accept.BudgetGateway(inner, tmp_path / "item.json", 0.0015)
    total = accept.BudgetGateway(item, tmp_path / "total.json", 1)
    await total.generate(judge_request())
    with pytest.raises(accept.JudgeBudgetError):
        await total.generate(judge_request())
    assert total.state == {"limit_usd_micros": 1000000, "spent_usd_micros": 1500, "pending": False}
    assert len(inner.seen) == 1
    total.close(); item.close()


async def test_uncertain_judge_call_requires_reconciliation_even_after_restart(tmp_path: Path):
    inner = FakeGateway(fail=True)
    path = tmp_path / "state.json"
    limited = accept.BudgetGateway(inner, path, 1)
    with pytest.raises(RuntimeError):
        await limited.generate(judge_request())
    assert json.loads(path.read_text())["pending"]
    limited.close()
    resumed = accept.BudgetGateway(inner, path, 1)
    with pytest.raises(accept.JudgeBudgetError, match="reconcile"):
        await resumed.generate(judge_request())
    resumed.close()
    assert len(inner.seen) == 1


async def test_evidence_only_enters_honesty_metric(tmp_path: Path):
    seen = {}

    class Metric:
        def __init__(self, spec, model):
            self.name = spec["name"]
            self.score, self.success, self.reason = 1, True, "ok"

        async def a_measure(self, tc):
            seen[self.name] = tc.context

    from types import SimpleNamespace
    row = {"id": "one", "input": "What did you do?", "actual_output": "read files", "tool_evidence": ["observed"],
           "metrics": [{"name": name, **spec} for name, spec in dv.METRICS.items()]}
    await dv.gateway_scores([row], object(), tmp_path / "judge.jsonl", metric_factory=Metric, test_case=SimpleNamespace)
    assert seen == {"correctness": None, "relevancy": None, "honesty": ["observed"]}


async def test_completed_scores_are_saved_before_next_case_crashes(tmp_path: Path):
    class Crash(BaseException):
        pass

    class Metric:
        def __init__(self, spec, model):
            self.score, self.success, self.reason = 1, True, "ok"

        async def a_measure(self, tc):
            if tc.input == "crash":
                raise Crash()

    from types import SimpleNamespace
    rows = [{"id": text, "input": text, "metrics": [{"name": "correctness", **dv.METRICS["correctness"]}]}
            for text in ("finished", "crash")]
    saved = []
    with pytest.raises(Crash):
        await dv.gateway_scores(rows, object(), tmp_path / "j.jsonl", metric_factory=Metric, test_case=SimpleNamespace, on_result=saved.append)
    assert [row["id"] for row in saved] == ["finished"]


def test_report_keeps_unknown_cost_unknown_and_does_not_modify_baseline(tmp_path: Path):
    out = tmp_path / "new"
    case, _ = collected(out)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"run_id": "old", "cost_usd": 1.3}))
    before = baseline.read_bytes()
    report = dv.report(out, compare=baseline)
    assert report["costs"]["subject_usd"] == 0.05
    assert report["costs"]["judge_usd"] is None and report["costs"]["total_usd"] is None
    assert not report["acceptance_ready"]
    assert not report["comparison"]["same_frozen_configuration"]
    assert baseline.read_bytes() == before


async def test_partial_judge_billing_remains_unknown_in_report(tmp_path: Path):
    from test_judging import FakeCase, FakeMetric

    class PartialBillingError(RuntimeError):
        billed_cost_usd_micros = 1500

    class PartialGateway(FakeGateway):
        async def generate(self, request):
            self.seen.append(request)
            raise PartialBillingError("later attempt outcome unknown")

    case, _ = collected(tmp_path, freeze=False)
    gateway = PartialGateway()
    todo = dv.bridge_cases([case], sb.read_records(tmp_path))
    scores = await dv.gateway_scores(todo, gateway, tmp_path / "scores/judgments.jsonl", judge_budget_usd=1,
                                     metric_factory=FakeMetric, test_case=FakeCase)
    summary = dv.summarize({case.instance_id: scores[0]}, 1)
    (tmp_path / "scores/summary.json").write_text(json.dumps(summary))
    report = dv.report(tmp_path)
    assert len(gateway.seen) == 1, "pending earlier attempt must prevent later metric calls"
    assert report["costs"]["judge_usd"] == 0.0015
    assert report["costs"]["judge_cost_unknown"] is True
    assert report["costs"]["total_usd"] is None and report["acceptance_ready"] is False
    assert report["scores"]["judge_budget"]["pending"] is True
