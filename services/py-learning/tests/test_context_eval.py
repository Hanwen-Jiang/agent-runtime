"""S-11 验收“压缩后的 Probe 通过率与重复操作率不劣于‘只做 mask’的基线”：评测链路（假模型）与真模型的门槛。

- 每条样本按 mask_only 与 full 两种策略压缩，被测模型只凭压缩后的上下文答 Probe 题、给出下一步动作。
- 报告给出两种策略的 Probe 通过率与重复操作率，以及门槛是否通过；留出集只给汇总。
- 真模型的门槛要 Model Gateway：设了 AR_EVAL_GATEWAY_FACTORY 才跑。
- T-01：报告写明每条样本在两种策略下走到了阶梯的哪一步；T-02：--repeat 重复跑，门槛按中位数判，每次的结果都保留。
"""

from __future__ import annotations

import json
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


# --- T-01：报告写明压缩走到了哪一步 ---


@pytest.mark.parametrize("split", ["dev", "holdout"])
async def test_the_report_counts_samples_that_were_summarized(tmp_path: Path, split: str) -> None:
    root = write_samples(tmp_path / "set", [context_sample("c1", split=split), context_sample("c2", split=split, work_mode="research")])
    subject = ScriptedContextSubject(CompactionGateway(), answer_fn=lexical, action_fn=redo_last_read)
    report = await run_context_eval(load_context_samples(root), subject, split=split, work_dir=tmp_path / "work")
    assert report.metrics["full"]["summarized"] == 2 and report.metrics["mask_only"]["summarized"] == 0
    assert "摘要" in report.summary()
    if split == "dev":
        rows = {(r["id"], r["strategy"]): r for r in report.samples}
        assert all("summarize" in rows[(sid, "full")]["applied"] for sid in ("c1", "c2"))
        assert all("summarize" not in rows[(sid, "mask_only")]["applied"] for sid in ("c1", "c2"))


# --- T-02：重复跑，给出中位数与范围 ---

ANSWERS = {"端口": "6379", "测试": "test_checkout", "fixture": "fixtures/orders.json"}


class RunScriptedSubject(ScriptedContextSubject):
    """mask_only 每次都答对；full 第 i 次（从 0 数）答不答得对由 full_right[i] 决定。看到摘要标记就算 full。"""

    def __init__(self, full_right: list[bool]) -> None:
        marker = {"intent": ["SUMMARY-MARKER"], "decisions": [], "artifacts": [], "open": [], "next": []}
        super().__init__(CompactionGateway(summary=marker), action_fn=redo_last_read)
        self.full_right = full_right
        self.full_calls = 0

    async def answer(self, messages, questions):  # type: ignore[no-untyped-def]
        context = text_of(messages)
        right = True
        if "SUMMARY-MARKER" in context:
            right = self.full_right[self.full_calls]
            self.full_calls += 1
        return [next((a for k, a in ANSWERS.items() if k in q), "") if right else "不知道" for q in questions]


async def test_repeated_runs_gate_on_the_median_and_keep_every_run(tmp_path: Path) -> None:
    root = write_samples(tmp_path / "set", [context_sample("c1")])
    subject = RunScriptedSubject(full_right=[False, True, True])
    report = await run_context_eval(load_context_samples(root), subject, split="dev", work_dir=tmp_path / "work", repeat=3)
    assert len(report.runs) == 3, "每次的结果都保留"
    assert [run["full"]["probe_pass_rate"] for run in report.runs] == [0.0, 1.0, 1.0]
    assert report.metrics["full"]["probe_pass_rate"] == 1.0, "metrics 取中位数"
    assert report.ranges["full"]["probe_pass_rate"] == (0.0, 1.0)
    assert report.gate_passed, "门槛按中位数判"
    assert sorted({r["run"] for r in report.samples}) == [1, 2, 3] and len(report.samples) == 6
    assert "0%–100%" in report.summary()
    data = report.to_json()
    assert data["repeat"] == 3 and len(data["runs"]) == 3 and data["ranges"]["full"]["probe_pass_rate"] == [0.0, 1.0]


async def test_a_worse_median_fails_the_gate_even_if_one_run_passes(tmp_path: Path) -> None:
    root = write_samples(tmp_path / "set", [context_sample("c1")])
    subject = RunScriptedSubject(full_right=[True, False, False])
    report = await run_context_eval(load_context_samples(root), subject, split="dev", work_dir=tmp_path / "work", repeat=3)
    assert report.metrics["full"]["probe_pass_rate"] == 0.0 and report.ranges["full"]["probe_pass_rate"] == (0.0, 1.0)
    assert not report.gate_passed


async def test_holdout_repeats_report_only_aggregates(tmp_path: Path) -> None:
    root = write_samples(tmp_path / "set", [context_sample("c1", split="holdout")])
    report = await run_context_eval(load_context_samples(root), RunScriptedSubject([True, True]), split="holdout", work_dir=tmp_path / "work", repeat=2)
    assert report.samples == [] and len(report.runs) == 2
    assert "c1" not in json.dumps(report.to_json(), ensure_ascii=False)


def test_the_cli_repeats_and_writes_every_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = write_samples(tmp_path / "set", [context_sample("c1")])
    reports = tmp_path / "reports"
    code = main(["context", "--eval-set", str(root), "--subject", "lexical", "--work-dir", str(tmp_path / "work"), "--repeat", "2", "--reports-dir", str(reports)])
    assert code == 0, capsys.readouterr().err
    [path] = list(reports.glob("context-*.json"))
    data = json.loads(path.read_text("utf-8"))
    assert data["repeat"] == 2 and len(data["runs"]) == 2
    assert "2 次" in capsys.readouterr().out


@pytest.mark.parametrize("repeat", ["0", "-1", "x"])
def test_the_cli_rejects_a_bad_repeat_count(tmp_path: Path, repeat: str) -> None:
    root = write_samples(tmp_path / "set", [context_sample("c1")])
    assert main(["context", "--eval-set", str(root), "--subject", "lexical", "--repeat", repeat]) == 2


@pytest.mark.skipif(not os.environ.get("AR_EVAL_GATEWAY_FACTORY"), reason="真模型的门槛要 Model Gateway（设 AR_EVAL_GATEWAY_FACTORY）")
async def test_full_is_not_worse_than_mask_only_on_a_real_model(tmp_path: Path) -> None:
    from agentruntime_learning.evals.worker_subject import context_subject

    samples = [s for s in load_context_samples(DEFAULT_EVAL_SET) if s.split == "dev"]
    report = await run_context_eval(samples, context_subject(), split="dev", work_dir=tmp_path)
    assert report.gate_passed, report.summary()
