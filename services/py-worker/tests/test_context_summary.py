"""S-11：增量摘要提示（附录 G.2 第 4 步；计划 S-11 交付“固定分区；只合并新截断的片段”，难点“精确状态不进摘要”
“按工作模式区分”）。"""

from __future__ import annotations

import json
from pathlib import Path

from agentruntime_worker.context.builder import ContextBuilder, tenant_context
from agentruntime_worker.context.modes import WORK_MODES, policy_for
from agentruntime_worker.context.summary import SECTION_TITLES, SECTIONS, empty, merge, render, summary_prompt

from .context_support import TASK_ID, TENANT, ContextGateway, config, delta, filler, history, objects, request, text_of


def test_the_prompt_has_the_fixed_sections_and_only_the_new_segment() -> None:
    segment = history(5, chars=200)[3:]
    messages = summary_prompt(segment, first_step=3, goal="修复 test_checkout", policy=policy_for("coding", extra_must_keep=("失败测试的名字",)))
    text = text_of(messages)
    for section in SECTIONS:
        assert section in text, f"提示要求固定分区 {section}"
    assert "修复 test_checkout" in text
    assert filler(3, 200)[:30] in text and filler(4, 200)[:30] in text
    assert filler(0, 200)[:30] not in text, "之前的步骤不在提示里"
    assert "失败测试的名字" in text, "Task Plan 的 must_keep 写进提示"
    assert "第 3 步" in text, "片段标明步号"


def test_each_work_mode_asks_to_keep_different_things() -> None:
    prompts = {mode: text_of(summary_prompt(history(1, 100), first_step=0, goal="g", policy=policy_for(mode))) for mode in WORK_MODES}
    assert len(set(prompts.values())) == len(WORK_MODES)
    assert "引用" in prompts["research"] or "URL" in prompts["research"]
    assert "退出码" in prompts["coding"] or "错误原文" in prompts["coding"]


def test_merge_appends_without_rewriting() -> None:
    first = merge(empty(), delta(intent=["修复 test_checkout"], decisions=["先读 checkout 模块"]))
    second = merge(first, delta(decisions=["先读 checkout 模块", "固定 tz 为 UTC"], next=["跑 make test"]))
    assert second["decisions"] == ["先读 checkout 模块", "固定 tz 为 UTC"], "已有条目原样保留，新的追加，重复的去掉"
    assert second["intent"] == ["修复 test_checkout"] and second["next"] == ["跑 make test"]
    rendered = render(second, covered=12)
    assert all(title in rendered for title in SECTION_TITLES.values()) and "12" in rendered


async def test_later_summaries_only_see_newly_truncated_steps(tmp_path: Path) -> None:
    provider = objects(tmp_path)
    gateway = ContextGateway(summaries=[delta(decisions=["第一段的决策"]), delta(decisions=["第二段的决策"])])
    b = ContextBuilder(gateway, provider, config(effective_tokens=4_000))
    await b.build(request(history(40, chars=600)))
    await b.build(request(history(70, chars=600)))
    first, second = gateway.of("summary")
    first_text, second_text = text_of(list(first.generation.messages)), text_of(list(second.generation.messages))
    assert filler(0, 600)[:30] in first_text
    assert filler(0, 600)[:30] not in second_text, "第二次只给新截断的片段"
    assert "第一段的决策" not in second_text, "不把已有摘要交给模型重写"
    stored = json.loads((await provider.bind(tenant_context(TENANT)).get(f"context/{TASK_ID}/r1/fix/summary.json")).data)
    assert stored["summary"]["decisions"] == ["第一段的决策", "第二段的决策"]
    assert stored["covered"] > 0


async def test_a_bad_summary_is_retried_once_then_nothing_is_dropped(tmp_path: Path) -> None:
    gateway = ContextGateway(summaries=["不是 JSON", {"intent": "不是列表"}])
    steps = history(40, chars=600)
    built = await ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=4_000)).build(request(steps))
    assert len(gateway.of("summary")) == 2
    assert "summarize" not in built.applied and "mask" in built.applied
    text = text_of(built.messages)
    assert all(f"step-{i}.json" in text or filler(i, 600)[:40] in text for i in range(40)), "摘要失败时每一步仍有占位符或原文"


async def test_summary_calls_use_the_configured_tier_and_structured_output(tmp_path: Path) -> None:
    gateway = ContextGateway(summaries=[delta(intent=["修复"])])
    await ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=4_000, summary_tier="light@low")).build(request(history(40, chars=600)))
    [call] = gateway.of("summary")
    assert call.call.tier == "light@low"
    assert call.generation.output_schema.value.struct_value["required"] is not None
