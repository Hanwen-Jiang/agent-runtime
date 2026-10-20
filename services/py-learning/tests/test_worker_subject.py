"""S-09 验收：Worker 的 Planner 接进 S-08 的评测。

真模型上的门槛（Triage 准确率 ≥ 90%、Stage 的 schema 通过率 100%、规则违例为 0、replan 不重做已验证的 Node）
要等 Model Gateway 接好：设了 AR_EVAL_GATEWAY_FACTORY 才跑，否则跳过。假模型上能证明的是“结构与规则由代码兜底”。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentruntime_learning.evals.__main__ import main
from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET, load_eval_set
from agentruntime_learning.evals.runner import run_eval
from agentruntime_learning.evals.subjects import load_subject
from agentruntime_learning.evals.worker_subject import ENV_GATEWAY_FACTORY, EVAL_TENANT, WorkerPlannerSubject

from fake_gateway import ScriptedGateway

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


@pytest.mark.skipif(not os.environ.get(ENV_GATEWAY_FACTORY), reason=f"真模型的验收跑分要等 Model Gateway 接好：设 {ENV_GATEWAY_FACTORY} 后再跑")
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
