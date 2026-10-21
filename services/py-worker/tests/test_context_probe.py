"""S-11：Compaction Probe（附录 G.6；计划 S-11 交付“从 Task State 与 Task Plan 中抽 3 到 5 个关键事实提问并判分；
答错就补回原文片段”）。"""

from __future__ import annotations

from pathlib import Path

from agentruntime_worker.context.builder import ContextBuilder
from agentruntime_worker.context.modes import policy_for
from agentruntime_worker.context.probe import BLANK, MAX_FACTS, MIN_FACTS, ProbeFact, grade, select_facts

from .context_support import (
    TASK_PLAN,
    TASK_STATE,
    ContextGateway,
    config,
    delta,
    history,
    objects,
    questions_of,
    read_step,
    request,
    text_of,
    wrong_answers,
)


def test_three_to_five_facts_are_drawn_from_the_task_state_and_plan() -> None:
    facts = select_facts(TASK_STATE, TASK_PLAN, policy_for("coding"))
    assert MIN_FACTS <= len(facts) <= MAX_FACTS
    for fact in facts:
        assert fact.answer and fact.answer in fact.text
        assert BLANK in fact.question() and fact.answer not in fact.question()
    answers = {f.answer for f in facts}
    assert answers & {"6379", "obj://t1/artifacts/patch-checkout.diff", "fixtures/orders.json", "2.5", "482"}, "优先挖数字、路径、ID"


def test_too_few_facts_gives_what_there_is() -> None:
    sparse = {"task_id": TASK_STATE["task_id"], "session_id": TASK_STATE["session_id"], "goal": "回答问题", "done_when": "给出答案"}
    assert len(select_facts(sparse, None, policy_for(None))) < MIN_FACTS


def test_grading_normalizes_text_but_numbers_must_match() -> None:
    port = ProbeFact(category="fact", text="测试依赖本地 Redis 的 6379 端口", answer="6379")
    assert grade(port, " 6379 ") and grade(port, "端口是 6379")
    assert not grade(port, "6380") and not grade(port, "63790")
    path = ProbeFact(category="open_question", text="fixtures/orders.json 是否需要更新", answer="fixtures/orders.json")
    assert grade(path, "Fixtures/Orders.json")


async def test_a_probe_follows_each_summarize(tmp_path: Path) -> None:
    gateway = ContextGateway(summaries=[delta(intent=["修复 test_checkout"])])
    built = await ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=4_000)).build(request(history(40, chars=600)))
    assert "summarize" in built.applied
    [probe] = gateway.of("probe")
    assert gateway.requests.index(probe) > gateway.requests.index(gateway.of("summary")[0]), "先摘要再抽查"
    assert MIN_FACTS <= len(questions_of(probe)) <= MAX_FACTS
    assert len(built.probes) == len(questions_of(probe)) and all(p.passed for p in built.probes), "事实在 status 里，按词面作答能答对"


async def test_wrong_answers_bring_back_the_original_snippet(tmp_path: Path) -> None:
    """答错的事实补回原文：history 里提到它的那一步（已被摘要拿掉）的观察摘录回到上下文里。"""
    clue = read_step(1, chars=300)
    clue["observation"]["content"] = "docker-compose.yml 里 redis 映射到 6379 端口；" + clue["observation"]["content"]
    steps = [read_step(0, 600), clue, *history(40, chars=600)[2:]]
    gateway = ContextGateway(summaries=[delta(intent=["修复 test_checkout"])], probes=[wrong_answers])
    built = await ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=4_000)).build(request(steps))
    assert built.probes and not any(p.passed for p in built.probes)
    text = text_of(built.messages)
    assert "docker-compose.yml 里 redis 映射到 6379 端口" in text, "补回提到该事实的原文片段"


async def test_no_probe_without_summarize_or_reset(tmp_path: Path) -> None:
    gateway = ContextGateway()
    built = await ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=4_000)).build(request(history(12, chars=600)))
    assert built.applied == ["mask"] and gateway.of("probe") == [] and built.probes == []


async def test_the_probe_can_be_turned_off(tmp_path: Path) -> None:
    gateway = ContextGateway(summaries=[delta(intent=["修复"])])
    built = await ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=4_000, probe=False)).build(request(history(40, chars=600)))
    assert "summarize" in built.applied and gateway.of("probe") == []


async def test_probe_questions_reach_the_model_with_the_compressed_context(tmp_path: Path) -> None:
    gateway = ContextGateway(summaries=[delta(intent=["修复 test_checkout"])])
    built = await ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=4_000)).build(request(history(40, chars=600)))
    [probe] = gateway.of("probe")
    sent = [m.SerializeToString() for m in probe.generation.messages[:-1]]
    assert sent == [m.SerializeToString() for m in built.messages], "抽查时给的就是压缩后的上下文"
    assert all(q.count(BLANK) == 1 for q in questions_of(probe))
    assert text_of(built.messages)
