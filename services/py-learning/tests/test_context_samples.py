"""S-11 验收“在 S-08 中加入长任务样本”的可自动检查部分：样本合格、覆盖每种工作模式、确实长到会触发摘要
（T-01：只做 mask 之后仍超过 summarize_at，加载时就检查）。

标注（Probe 题与已完成的动作）要经人确认：2026-10-21 首轮审阅后 7 条全部确认（review.status = confirmed）。
以后改了标注或新增样本，先回到草稿（review.status = draft），test_every_context_label_is_confirmed 会失败，直到人再次确认。
"""

from __future__ import annotations

import json
from collections import Counter
from functools import cache
from pathlib import Path

import pytest

from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET, EvalSetError
from agentruntime_learning.evals.context import ContextSample, ExtractiveGateway, build_context, load_context_samples, masked_tokens, summarize_threshold
from agentruntime_worker.context.modes import WORK_MODES
from agentruntime_worker.context.tokens import estimate

from context_support import context_sample, write_samples


@cache
def samples() -> tuple[ContextSample, ...]:
    return tuple(load_context_samples(DEFAULT_EVAL_SET))


def test_masking_alone_leaves_every_sample_over_the_summarize_threshold() -> None:
    """T-01：只做 mask 之后仍超过 summarize_at，full 才一定会摘要；不然 full 与 mask_only 的差别只剩随机波动。"""
    for s in samples():
        assert masked_tokens(s) > summarize_threshold(s), f"{s.id}：mask 之后约 {masked_tokens(s)} token，不超过 {summarize_threshold(s):.0f}"


async def test_full_summarizes_every_sample(tmp_path: Path) -> None:
    """离线的抽取式摘要下，每条样本在 full 策略里都走到 summarize（摘要不合格时 Context Builder 会放弃摘要）。"""
    for s in samples():
        built = await build_context(s, "full", ExtractiveGateway(), tmp_path / s.id)
        assert "summarize" in built.applied, (s.id, built.applied)


def test_the_threshold_follows_the_task_plan_override(tmp_path: Path) -> None:
    plan = {"triage": {"level": "full"}, "horizon": "per_stage", "context": {"work_mode": "coding", "summarize_at": 0.5}}
    root = write_samples(tmp_path, [context_sample("c_default"), context_sample("c_override", task_plan=plan)])
    default, overridden = load_context_samples(root)
    assert summarize_threshold(default) == 0.7 * default.effective_tokens
    assert summarize_threshold(overridden) == 0.5 * overridden.effective_tokens


def test_a_sample_that_masking_alone_shrinks_enough_is_rejected_at_load(tmp_path: Path) -> None:
    root = write_samples(tmp_path, [context_sample("c_roomy", effective_tokens=40_000), context_sample("c_tight")])
    with pytest.raises(EvalSetError) as err:
        load_context_samples(root)
    text = "\n".join(err.value.problems)
    assert "c_roomy" in text and "summarize_at" in text, text
    assert "c_tight" not in text


def test_every_work_mode_has_a_long_task_sample() -> None:
    counts = Counter(s.work_mode for s in samples())
    assert set(WORK_MODES) <= set(counts), f"缺少：{set(WORK_MODES) - set(counts)}"


def test_both_splits_have_samples() -> None:
    assert {s.split for s in samples()} == {"dev", "holdout"}
    assert sum(1 for s in samples() if s.split == "dev") >= sum(1 for s in samples() if s.split == "holdout")


def test_samples_are_long_enough_to_summarize() -> None:
    """按样本的有效长度，原始轨迹超过 summarize 的默认阈值（70%），full 策略一定会摘要。"""
    for s in samples():
        tokens = sum(estimate(json.dumps(step, ensure_ascii=False)) for step in s.history)
        assert tokens > 0.7 * s.effective_tokens * 1.5, f"{s.id}：轨迹约 {tokens} token，不够长"
        assert len(s.history) >= 30, s.id


def test_labels_are_grounded_in_the_trajectory() -> None:
    for s in samples():
        assert 3 <= len(s.probes) <= 8, s.id
        source = json.dumps(s.history, ensure_ascii=False) + json.dumps(s.task_state, ensure_ascii=False)
        for probe in s.probes:
            assert probe["answer"] in source, f"{s.id}：答案 {probe['answer']} 不在轨迹或 Task State 里"
        actions = [(step["tool"], json.dumps(step["args"], sort_keys=True)) for step in s.history]
        for done in s.completed:
            assert (done["tool"], json.dumps(done["args"], sort_keys=True)) in actions, f"{s.id}：已完成的动作 {done} 不在轨迹里"


def test_every_context_label_is_confirmed() -> None:
    assert [s.id for s in samples() if not s.confirmed] == []


def test_malformed_samples_are_rejected(tmp_path: Path) -> None:
    bad = [
        context_sample("c_mode", work_mode="gossip"),
        context_sample("c_answer", probes=[{"question": "端口？", "answer": "6380"}] * 3),
        context_sample("c_done", completed=[{"tool": "repo_read", "args": {"path": "nowhere.py"}}]),
    ]
    root = write_samples(tmp_path, bad)
    (root / "context_samples" / "c_name.json").write_text(json.dumps(context_sample("c_other"), ensure_ascii=False), "utf-8")
    with pytest.raises(EvalSetError) as err:
        load_context_samples(root)
    text = "\n".join(err.value.problems)
    for sid in ("c_mode", "c_answer", "c_done", "c_name"):
        assert sid in text, f"{sid} 的问题没报出来：{text}"


def test_a_set_without_context_samples_loads_empty(tmp_path: Path) -> None:
    assert load_context_samples(tmp_path) == []
