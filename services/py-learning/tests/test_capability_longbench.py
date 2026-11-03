"""LongBench v2（capability.evals.longbench）：缓存拆分、子集、官方模板与答案提取、截掉中间。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentruntime_learning.capability.base import role_enum
from agentruntime_learning.capability.evals import longbench as lb

from capability_datasets_support import ScriptGateway, text_of


def row(i: int, length: str = "short", answer: str = "D", context: str = "") -> dict:
    return {"_id": f"66fcffd9bb02136c067c94c{i}", "domain": "Long In-context Learning", "sub_domain": "New language translation",
            "difficulty": "hard" if i % 2 else "easy", "length": length, "question": f" Translate sentence {i}. ",
            "choice_A": "a", "choice_B": "b", "choice_C": "c", "choice_D": "d", "answer": answer,
            "context": context or f"Grammar book {i}. " * 20}


@pytest.fixture
def ev(tmp_path: Path) -> lb.LongBenchEval:
    source = tmp_path / "data.json"
    source.write_text(json.dumps([row(1), row(2, answer="B"), row(3, "medium"), row(4, "long")], ensure_ascii=False, indent=1), "utf-8")
    return lb.make(cache=tmp_path / "cache", source=source)


def test_the_cache_splits_items_and_keeps_only_the_short_subset(ev: lb.LongBenchEval, tmp_path: Path) -> None:
    assert sorted(ev.index()) == ["66fcffd9bb02136c067c94c1", "66fcffd9bb02136c067c94c2"]
    assert (tmp_path / "cache" / "items" / "66fcffd9bb02136c067c94c3.json").exists()
    assert "context" not in ev.items(1, 0)[0].payload, "题目元数据里不带长上下文"
    both = lb.make(cache=tmp_path / "cache", lengths=("short", "medium"), difficulties=("hard",))
    assert sorted(both.index()) == ["66fcffd9bb02136c067c94c1", "66fcffd9bb02136c067c94c3"]
    assert both.version.endswith(":short+medium:hard")
    with pytest.raises(ValueError):
        lb.make(lengths=("tiny",))


async def test_the_official_prompt_and_answer_extraction(ev: lb.LongBenchEval) -> None:
    item = next(i for i in ev.items(5, 0) if i.id.endswith("c1"))
    gw = ScriptGateway(lambda r: "**The correct answer is (D)**")
    res = await ev.run_item(item, gw, candidate="a6api/gemini-3.5-flash@2026-10")
    assert res.score == 1.0 and res.record["pred"] == "D" and not res.record["truncated"]
    req = gw.requests[0]
    assert req.call.role == role_enum("transform") and len(req.generation.messages) == 1
    prompt = text_of(req)
    assert prompt.startswith("Please read the following text") and "What is the correct answer to this question: Translate sentence 1." in prompt
    assert "(D) d\n\nFormat your response as follows" in prompt
    wrong = await ev.run_item(item, ScriptGateway(lambda r: "The correct answer is A"), candidate="x")
    assert wrong.score == 0.0 and wrong.record["pred"] == "A"


def test_extraction_follows_pred_py() -> None:
    assert lb.extract_answer("The correct answer is (B).") == "B" and lb.extract_answer("The correct answer is C") == "C"
    assert lb.extract_answer("I think (B)") is None


def test_long_contexts_lose_their_middle() -> None:
    text = "头" * 100 + "中" * 1000 + "尾" * 100
    cut, did = lb.truncate_middle(text, 600)
    assert did and cut.startswith("头" * 100) and cut.endswith("尾" * 100) and len(cut.encode()) <= 600
    prompt, did = lb.build_prompt(row(9, context="x" * 50_000), max_context_tokens=1000)
    assert did and len(prompt.encode()) <= 4000


async def test_a_missing_item_file_is_an_error(ev: lb.LongBenchEval, tmp_path: Path) -> None:
    item = ev.items(1, 0)[0]
    (tmp_path / "cache" / "items" / f"{item.id}.json").unlink()
    res = await ev.run_item(item, ScriptGateway(lambda r: "x"), candidate="x")
    assert res.score == 0.0 and res.error and res.error.startswith("FileNotFoundError")
