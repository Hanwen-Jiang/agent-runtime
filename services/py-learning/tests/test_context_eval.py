"""S-11 验收“压缩后的 Probe 通过率与重复操作率不劣于‘只做 mask’的基线”：评测链路（假模型）与真模型的门槛。

- 每条样本按 mask_only 与 full 两种策略压缩，被测模型只凭压缩后的上下文答 Probe 题、给出下一步动作。
- 报告给出两种策略的 Probe 通过率与重复操作率，以及门槛是否通过；留出集只给汇总。
- 真模型的门槛要 Model Gateway：设了 AR_EVAL_GATEWAY_FACTORY 才跑。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from agentruntime_learning.evals.__main__ import main
from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET
from agentruntime_learning.evals.context import STRATEGIES, compact, load_context_samples, run_context_eval
from agentruntime_worker.context.tokens import estimate

from context_support import CompactionGateway, ScriptedContextSubject, context_sample, filler, text_of, write_samples


def lexical(context: str, question: str) -> str:
    """答案按词面找：端口找数字，测试找 test_ 开头的词，fixture 找路径。"""
    patterns = {"端口": r"\b\d{4}\b", "测试": r"\btest_\w+", "fixture": r"fixtures/[\w.]+"}
    for key, pattern in patterns.items():
        if key in question:
            found = re.findall(pattern, context)
            return found[0] if found else ""
    return ""


def redo_last_read(context: str) -> tuple[str, dict]:
    """看不到“已读过 docker-compose.yml”就再读一遍。"""
    return ("repo_read", {"path": "docker-compose.yml"}) if "docker-compose.yml" not in context else ("repo_edit", {"path": "src/checkout.py"})


async def test_both_strategies_compact_to_the_effective_length(tmp_path: Path) -> None:
    [sample] = load_context_samples(write_samples(tmp_path / "set", [context_sample()]))
    masking, summarizing = CompactionGateway(), CompactionGateway()
    masked = await compact(sample, "mask_only", masking, tmp_path / "w1")
    full = await compact(sample, "full", summarizing, tmp_path / "w2")
    assert masking.requests == [], "mask_only 不调用模型"
    assert any(r.call.node_id.endswith("#summary") for r in summarizing.requests), "full 用 S-11 的增量摘要"
    assert estimate(text_of(full)) <= 0.7 * sample.effective_tokens
    assert estimate(text_of(full)) < estimate(text_of(masked))
    assert filler(0)[:30] not in text_of(masked), "mask_only 也遮蔽较早的观察"


async def test_the_report_compares_full_against_mask_only(tmp_path: Path) -> None:
    root = write_samples(tmp_path / "set", [context_sample("c1"), context_sample("c2", work_mode="research")])
    gateway = CompactionGateway()
    subject = ScriptedContextSubject(gateway, answer_fn=lexical, action_fn=redo_last_read)
    report = await run_context_eval(load_context_samples(root), subject, split="dev", work_dir=tmp_path / "work")
    assert set(report.metrics) == set(STRATEGIES)
    for strategy in STRATEGIES:
        metrics = report.metrics[strategy]
        assert 0.0 <= metrics["probe_pass_rate"] <= 1.0 and 0.0 <= metrics["repeat_rate"] <= 1.0 and metrics["samples"] == 2
    full, masked = report.metrics["full"], report.metrics["mask_only"]
    assert report.gate_passed == (full["probe_pass_rate"] >= masked["probe_pass_rate"] and full["repeat_rate"] <= masked["repeat_rate"])
    assert len(report.samples) == 4, "每条样本、每种策略一行"
    assert {"id", "strategy", "probes", "repeated"} <= set(report.samples[0])
    assert "mask_only" in report.summary() and "full" in report.summary()


async def test_a_worse_full_strategy_fails_the_gate(tmp_path: Path) -> None:
    root = write_samples(tmp_path / "set", [context_sample("c1")])
    gateway = CompactionGateway(summary={"intent": ["SUMMARY-MARKER"], "decisions": [], "artifacts": [], "open": [], "next": []})
    # 看到摘要就答不出：full 比 mask_only 差，门槛不过。
    subject = ScriptedContextSubject(gateway, answer_fn=lambda context, q: "" if "SUMMARY-MARKER" in context else lexical(context, q), action_fn=redo_last_read)
    report = await run_context_eval(load_context_samples(root), subject, split="dev", work_dir=tmp_path / "work")
    assert report.metrics["full"]["probe_pass_rate"] < report.metrics["mask_only"]["probe_pass_rate"]
    assert not report.gate_passed


async def test_holdout_reports_only_the_summary(tmp_path: Path) -> None:
    root = write_samples(tmp_path / "set", [context_sample("c1", split="holdout")])
    subject = ScriptedContextSubject(CompactionGateway(), answer_fn=lexical, action_fn=redo_last_read)
    report = await run_context_eval(load_context_samples(root), subject, split="holdout", work_dir=tmp_path / "work")
    assert report.samples == [] and report.metrics["full"]["samples"] == 1


def test_the_cli_runs_the_lexical_baseline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = write_samples(tmp_path / "set", [context_sample("c1")])
    code = main(["context", "--eval-set", str(root), "--subject", "lexical", "--work-dir", str(tmp_path / "work")])
    out = capsys.readouterr().out
    assert code == 0 and "mask_only" in out and "full" in out


@pytest.mark.skipif(not os.environ.get("AR_EVAL_GATEWAY_FACTORY"), reason="真模型的门槛要 Model Gateway（设 AR_EVAL_GATEWAY_FACTORY）")
async def test_full_is_not_worse_than_mask_only_on_a_real_model(tmp_path: Path) -> None:
    from agentruntime_learning.evals.worker_subject import context_subject

    samples = [s for s in load_context_samples(DEFAULT_EVAL_SET) if s.split == "dev"]
    report = await run_context_eval(samples, context_subject(), split="dev", work_dir=tmp_path)
    assert report.gate_passed, report.summary()
