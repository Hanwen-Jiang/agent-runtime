"""S-09、S-10 验收：Worker 的 Planner 与 Reviewer 接进 S-08 的评测。

真模型上的门槛要经 Model Gateway（花钱、有随机性）：设了 AR_EVAL_GATEWAY_FACTORY 才跑，否则跳过。
- S-09（planner_subject）：Triage 准确率 ≥ 90%、Stage 的 schema 通过率 100%、规则违例为 0、replan 不重做已验证的 Node。
- S-10（worker_subject，Planner 加 Reviewer）：Reviewer 样本上 Verdict 准确率 ≥ 85%，“该升级却原地重试”的错误为 0。
假模型上能证明的是“结构与规则由代码兜底”。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentruntime_learning.evals.__main__ import main
from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET, load_eval_set
from agentruntime_learning.evals.model import ReviewerInput
from agentruntime_core.errors import DeniedError
from agentruntime_learning.evals.repeat import run_repeated
from agentruntime_learning.evals.runner import run_eval
from agentruntime_learning.evals.scoring import score_reviewer
from agentruntime_learning.evals.subjects import load_subject
from agentruntime_learning.evals.worker_subject import ENV_GATEWAY_FACTORY, EVAL_TENANT, WorkerPlannerSubject, WorkerSubject

from fake_gateway import ScriptedGateway
from support import reviewer_sample, task_case

SPLITS = ["dev", "holdout"]


@pytest.mark.parametrize("split", SPLITS)
async def test_structure_and_rules_hold_even_when_the_model_gets_everything_wrong(split: str) -> None:
    """模型的分诊与规划全不合格时：分诊按 full，规划两次不合格就回退到保守规划。schema 与规则照样 100% 合格。"""
    es = load_eval_set(DEFAULT_EVAL_SET)
    report = await run_eval(es, WorkerPlannerSubject(ScriptedGateway()), split=split, reports_dir=None, compare=None)
    planner = report.data["metrics"]["planner"]
    assert planner["schema_pass_rate"] == 1.0, report.summary()
    assert planner["rule_violations"] == 0, report.summary()
    assert report.data["subject"] == "worker-planner"


async def test_requests_carry_the_eval_tenant_and_usage_is_reported() -> None:
    es = load_eval_set(DEFAULT_EVAL_SET)
    gateway = ScriptedGateway(vendor="vendor_q")
    report = await run_eval(es, WorkerPlannerSubject(gateway), reports_dir=None, compare=None)
    assert gateway.requests and {r.tenant.tenant_id for r in gateway.requests} == {EVAL_TENANT}
    cost = report.data["metrics"]["cost"]
    assert cost["total_usd"] > 0
    assert cost["planner_cost_ratio"]["cases"] > 0 and cost["planner_cost_ratio"]["mean"] is not None


async def test_replan_cases_show_the_planner_what_to_keep() -> None:
    """replan 用例的上一轮（Reviewer 保留的 Node 与卡住的原因）要送到 Planner 的提示里。"""
    es = load_eval_set(DEFAULT_EVAL_SET)
    replans = [c for c in es.task_cases if c.planner_expect["triage"] == "replan" and c.split == "dev"]
    gateway = ScriptedGateway()
    await run_eval(es, WorkerPlannerSubject(gateway), reports_dir=None, compare=None)
    prompts = ["\n".join(m.text for m in r.generation.messages) for r in gateway.requests if not r.call.node_id.startswith("triage")]
    for case in replans:
        keep = (case.planner_input["last_round"]["verdict"].get("replan") or {}).get("keep_nodes", [])
        assert any(all(k in p for k in keep) and case.planner_input["message"] in p for p in prompts), case.id


def test_the_cli_runs_the_worker_planner_from_a_gateway_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv(ENV_GATEWAY_FACTORY, "fake_gateway:make")
    code = main(["run", "--subject", "agentruntime_learning.evals.worker_subject:planner_subject", "--reports-dir", str(tmp_path), "--compare", "none"])
    assert code == 0
    assert "worker-planner" in capsys.readouterr().out


def test_without_a_gateway_factory_the_subject_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_GATEWAY_FACTORY, raising=False)
    with pytest.raises(ValueError, match=ENV_GATEWAY_FACTORY):
        load_subject("agentruntime_learning.evals.worker_subject:planner_subject", load_eval_set(DEFAULT_EVAL_SET))


@pytest.mark.skipif(not os.environ.get(ENV_GATEWAY_FACTORY), reason=f"真模型的验收跑分要经 Model Gateway：设 {ENV_GATEWAY_FACTORY} 后再跑")
@pytest.mark.parametrize("split", SPLITS)
async def test_the_planner_meets_the_s09_thresholds_on_the_real_model(split: str) -> None:
    es = load_eval_set(DEFAULT_EVAL_SET)
    subject = load_subject("agentruntime_learning.evals.worker_subject:planner_subject", es)
    report = await run_eval(es, subject, split=split, reports_dir=None, compare=None, require_confirmed=True)
    planner = report.data["metrics"]["planner"]
    assert planner["triage_accuracy"] >= 0.9, report.summary()
    assert planner["schema_pass_rate"] == 1.0, report.summary()
    assert planner["rule_violations"] == 0, report.summary()
    assert not [r for r in report.data["failure_reasons"] if r["code"] == "keep_nodes"], "replan 不得重做已验证的 Node"

# --- S-10：Reviewer ---

RETRY_IN_PLACE = {
    "verdict": {
        "verdict": "repair",
        "reason": "再试一次",
        "repair": {"nodes": ["ghost"], "strategy": "retry_with_error"},
        "progress_check": {"criteria_met": False, "progress": False, "looping": False, "assumptions_hold": True, "next_step": "重试"},
    },
    "completion": 0.5,
    "state_update": {},
}
"""对抗性的模型：不管看到什么都说原地重试，Node 还是编的。"""

# 升级的 floor 能从结构化的输入算出来的样本：同一 Node 第 2 次失败、两次 replan 没有进展、预算用尽、写操作被策略拒绝；
# 2026-10-28 真模型实验第 3.2 条之后加的两条：本轮重试过仍是上一轮的同一错误（附录 B.7）、扇出后合并失败（附录 B.7）。
RULE_DECIDED = ("rs_repair_second_failure", "rs_early_replans", "rs_abort_budget_exhausted", "rs_denied_write", "rs_revise_same_error", "rs_fanout_conflict")


@pytest.mark.parametrize("split", SPLITS)
async def test_verdicts_match_the_schema_even_when_the_model_gets_everything_wrong(split: str) -> None:
    """Progress Check 两次都不合格时回退到规则：Verdict 照样 100% 合乎 schema，done 只来自本轮成功的 deliver 节点。"""
    es = load_eval_set(DEFAULT_EVAL_SET)
    report = await run_eval(es, WorkerSubject(ScriptedGateway()), split=split, reports_dir=None, compare=None)
    metrics = report.data["metrics"]
    assert metrics["reviewer"]["schema_pass_rate"] == 1.0, report.summary()
    assert metrics["rounds"]["schema_pass_rate"] == 1.0, report.summary()
    assert not [r for r in report.data["failure_reasons"] if r["code"] == "done_from_deliver"]
    assert report.data["subject"] == "worker"


async def test_code_floors_alone_stop_stuck_retries_where_the_rule_applies() -> None:
    """附录 B.8 的规则由代码判定：模型永远原地重试时，这几条样本照样没有“该升级却原地重试”，判定也在可接受的范围内。"""
    es = load_eval_set(DEFAULT_EVAL_SET)
    samples = {s.id: s for s in es.reviewer_samples}
    subject = WorkerSubject(ScriptedGateway(review=lambda _r: RETRY_IN_PLACE))
    for sample_id in RULE_DECIDED:
        cp = samples[sample_id].checkpoint
        out = await subject.review(ReviewerInput(task_state=cp.task_state, stage=cp.stage, results=cp.results, history=cp.history))
        score = score_reviewer(cp, out)
        assert not score.failed("escalation") and not score.failed("verdict") and score.schema_ok, (sample_id, out.verdict, score.failures)


async def test_the_reviewer_runs_on_the_eval_tenant_and_reports_usage() -> None:
    es = load_eval_set(DEFAULT_EVAL_SET)
    gateway = ScriptedGateway(review=lambda _r: RETRY_IN_PLACE)
    report = await run_eval(es, WorkerSubject(gateway), split="dev", reports_dir=None, compare=None)
    reviews = [r for r in gateway.requests if r.call.node_id.startswith("review")]
    assert reviews and {r.tenant.tenant_id for r in reviews} == {EVAL_TENANT}
    assert report.data["metrics"]["cost"]["total_usd"] > 0


def test_without_a_gateway_factory_the_worker_subject_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_GATEWAY_FACTORY, raising=False)
    with pytest.raises(ValueError, match=ENV_GATEWAY_FACTORY):
        load_subject("agentruntime_learning.evals.worker_subject:worker_subject", load_eval_set(DEFAULT_EVAL_SET))


@pytest.mark.skipif(not os.environ.get(ENV_GATEWAY_FACTORY), reason=f"真模型的验收跑分要经 Model Gateway：设 {ENV_GATEWAY_FACTORY} 后再跑")
@pytest.mark.parametrize("split", SPLITS)
async def test_the_reviewer_meets_the_s10_thresholds_on_the_real_model(split: str) -> None:
    es = load_eval_set(DEFAULT_EVAL_SET)
    subject = load_subject("agentruntime_learning.evals.worker_subject:worker_subject", es)
    report = await run_eval(es, subject, split=split, reports_dir=None, compare=None, require_confirmed=True)
    metrics = report.data["metrics"]
    assert metrics["reviewer"]["verdict_accuracy"] >= 0.85, report.summary()
    assert metrics["stuck_retry_errors"] == 0, report.summary()
    assert metrics["reviewer"]["schema_pass_rate"] == 1.0 and metrics["rounds"]["schema_pass_rate"] == 1.0, report.summary()


# --- 模型调用失败要看得见：Planner、Reviewer 会吞掉错误回退，分数就不再代表模型的判断 ---


def quota_exhausted(_request: object) -> dict:
    raise DeniedError("http_403", "可用额度不足")


async def test_failed_model_calls_are_counted_in_the_report(make_set) -> None:
    """2026-10-22 实测：供应商额度用完后每次调用都 403，Planner 回退到 full 规划、Reviewer 回退到规则判定，评测照样“跑完”。
    报告要写出这次有多少次模型调用失败、各是什么 code，摘要里给出提醒。"""
    es = load_eval_set(make_set([task_case("t1"), task_case("t2")], [reviewer_sample("s1")]))
    gateway = ScriptedGateway(triage=quota_exhausted, review=quota_exhausted)
    report = await run_eval(es, WorkerSubject(gateway, recipes={}), reports_dir=None, compare=None)
    metrics = report.data["metrics"]
    assert metrics["model_call_failures"] >= 3, "两次分诊加至少一次评审"
    assert report.data["model_call_failure_codes"].get("http_403") == metrics["model_call_failures"]
    assert "模型调用失败" in report.summary()


async def test_each_run_counts_only_its_own_failures(make_set) -> None:
    """同一个被测对象重复跑：每次报告只记本次的失败，汇总里看得出哪一次掺了回退。"""
    es = load_eval_set(make_set([task_case("t1")]))
    calls = {"n": 0}

    def fail_first_run(request: object) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            raise DeniedError("http_403", "可用额度不足")
        return {"kind": "chitchat", "task_family": None, "confidence": 0.9, "effect": "none", "irreversible_params_explicit": True}

    result = await run_repeated(es, WorkerPlannerSubject(ScriptedGateway(triage=fail_first_run)), split="dev", repeat=2, reports_dir=None, compare=None)
    assert [run.data["metrics"]["model_call_failures"] for run in result.runs] == [1, 0]
    assert result.data["metrics"]["model_call_failures"]["values"] == [1, 0]
    assert "模型调用失败" in result.summary()


async def test_subjects_without_a_gateway_report_no_failure_count(make_set) -> None:
    es = load_eval_set(make_set([task_case("t1")]))
    report = await run_eval(es, load_subject("baseline", es), reports_dir=None, compare=None)
    assert "model_call_failures" not in report.data["metrics"]
    assert "模型调用失败" not in report.summary()
