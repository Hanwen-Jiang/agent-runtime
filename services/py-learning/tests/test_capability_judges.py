"""RewardBench 2 与 JudgeBench（capability.evals.rewardbench2、judgebench）：被测模型当裁判，按标准答案判；控制位置偏差。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_learning.capability.base import role_enum
from agentruntime_learning.capability.evals import judgebench as jb
from agentruntime_learning.capability.evals import rewardbench2 as rb

from capability_support import ScriptGateway, text_of, write_jsonl

CANDIDATE = "a6api/claude-opus-5-5@2026-10"


def rb_row(i: str, subset: str = "Factuality") -> dict:
    return {"id": i, "prompt": f"Question {i}?", "chosen": [f"GOOD answer {i}"], "rejected": [f"bad one {i}", f"bad two {i}", f"bad three {i}"],
            "num_correct": 1, "num_incorrect": 3, "total_completions": 4, "models": ["m1", "m2", "m3", "m4"], "subset": subset,
            "additional_metadata": {"category": None, "correct": "ONE"}}


@pytest.fixture
def rb_source(tmp_path: Path) -> Path:
    rows = [rb_row("0"), rb_row("1"), rb_row("0", "Precise IF"), rb_row("7", "Math"),
            {**rb_row("9", "Ties"), "chosen": ["a", "b"], "rejected": ["c"] * 10}]
    return write_jsonl(tmp_path / "rb2.jsonl", rows)


def rating_gateway(scores: dict[str, str]) -> ScriptGateway:
    """按回答里的关键词打分：scores 是 关键词 → 判语。"""

    def answer(req: mg.GenerateRequest) -> str:
        text = text_of(req).split("[Response]")[1]
        return next(v for k, v in scores.items() if k in text)

    return ScriptGateway(answer)


def test_rewardbench2_keeps_the_five_subsets_and_unique_ids(rb_source: Path) -> None:
    ev = rb.make(source=rb_source)
    assert sorted(ev.rows()) == ["Factuality:0", "Factuality:1", "Math:7", "Precise IF:0"], "Ties 不收；id 带上子集（原数据里跨子集重复）"
    assert len({ev.rows()[i.id]["subset"] for i in ev.items(3, 5)}) == 3, "按子集轮流取"
    assert ev.version == f"hf@{rb.REVISION[:7]}" and ev.role == "judge"


async def test_ratings_mode_rates_each_answer_alone(rb_source: Path) -> None:
    ev = rb.make(source=rb_source)
    gw = rating_gateway({"GOOD": "Accurate and complete.\n9", "bad": "Wrong.\n3"})
    res = await ev.run_item(next(i for i in ev.items(9, 0) if i.id == "Factuality:0"), gw, candidate=CANDIDATE)
    assert res.score == 1.0 and res.error is None and res.record["ratings"] == [9, 3, 3, 3]
    assert len(gw.requests) == 4 and all(r.call.role == role_enum("judge") for r in gw.requests)
    assert [m.role for m in gw.requests[0].generation.messages] == [mg.CHAT_ROLE_USER], "官方的 ratings 只有用户消息"
    assert "GOOD answer 0" in text_of(gw.requests[0]) and "bad" not in text_of(gw.requests[0]), "一次只看一个回答"
    assert res.cost_usd == pytest.approx(0.004)


@pytest.mark.parametrize(("scores", "expected"), [
    ({"GOOD": "8", "bad one": "8", "bad": "2"}, 0.5),
    ({"GOOD": "5", "bad one": "6", "bad": "2"}, 0.0),
    ({"GOOD": "no number", "bad": "4"}, 0.0),
])
async def test_ratings_ties_and_missing_scores_follow_the_official_rule(rb_source: Path, scores: dict, expected: float) -> None:
    ev = rb.make(source=rb_source)
    res = await ev.run_item(next(i for i in ev.items(9, 0) if i.id == "Factuality:0"), rating_gateway(scores), candidate=CANDIDATE)
    assert res.score == expected and res.error is None


async def test_ratings_without_any_score_is_an_error(rb_source: Path) -> None:
    ev = rb.make(source=rb_source)
    res = await ev.run_item(ev.items(1, 0)[0], ScriptGateway(lambda r: "I cannot rate this."), candidate=CANDIDATE)
    assert res.score == 0.0 and "1～10" in (res.error or "")


async def test_four_way_puts_the_good_answer_in_two_places(rb_source: Path) -> None:
    ev = rb.make(source=rb_source, mode="four_way")
    item = ev.items(1, 0)[0]

    def pick_good(req: mg.GenerateRequest) -> str:
        user = req.generation.messages[1].text
        letter = re.search(r"Assistant ([ABCD])'s Answer\]\nGOOD", user).group(1)  # type: ignore[union-attr]
        return f"Explanation. [[{letter}]]"

    gw = ScriptGateway(pick_good)
    res = await ev.run_item(item, gw, candidate=CANDIDATE)
    places = [t["chosen_at"] for t in res.record["trials"]]
    assert res.score == 1.0 and len(set(places)) == 2 and places == [rb.LETTERS[p] for p in rb.positions(item.id, 2)]
    assert gw.requests[0].generation.messages[0].text == rb.FOUR_WAY_SYSTEM

    always_a = await ev.run_item(item, ScriptGateway(lambda r: "[[A]]"), candidate=CANDIDATE)
    assert always_a.score == 0.0, "总选同一个位置的裁判不能两次都对"


def test_parsers_match_the_official_ones() -> None:
    assert rb.parse_rating("Good.\n10") == 10 and rb.parse_rating("rating: 7 ") == 7 and rb.parse_rating("7/10 overall.") == -1
    assert rb.parse_choice("[[B]] or maybe [[A]]") == "A" and rb.parse_choice("none") == "error"
    with pytest.raises(ValueError):
        rb.make(mode="pairs")


# --- JudgeBench ---


def jb_row(pair: str, label: str) -> dict:
    return {"pair_id": pair, "original_id": 1, "source": "mmlu-pro-law", "question": f"Q {pair}", "response_model": "gpt-4o-2024-05-13",
            "response_A": "RIGHT" if label == "A>B" else "WRONG", "response_B": "WRONG" if label == "A>B" else "RIGHT", "label": label}


@pytest.fixture
def jb_root(tmp_path: Path) -> Path:
    write_jsonl(tmp_path / "gpt-00000-of-00001.jsonl", [jb_row("p1", "A>B"), jb_row("p2", "B>A")])
    write_jsonl(tmp_path / "claude-00000-of-00001.jsonl", [jb_row("c1", "A>B")])
    return tmp_path


def judge_by(rule) -> ScriptGateway:  # type: ignore[no-untyped-def]
    """rule(a 是对的回答吗, 第几次调用) → 判语。"""
    def answer(req: mg.GenerateRequest) -> str:
        user = req.generation.messages[1].text
        a_right = user.index("RIGHT") < user.index("WRONG")
        return rule(a_right, len(gw.requests))

    gw = ScriptGateway(answer)
    return gw


async def test_judgebench_asks_both_orders_and_wants_both_right(jb_root: Path) -> None:
    ev = jb.make(root=jb_root)
    assert sorted(ev.rows()) == ["gpt:p1", "gpt:p2"]
    item = next(i for i in ev.items(5, 0) if i.id == "gpt:p2")
    gw = judge_by(lambda a_right, n: "My final verdict: [[A>>B]]" if a_right else "[[B>A]]")
    res = await ev.run_item(item, gw, candidate=CANDIDATE)
    assert res.score == 1.0 and res.record["consistent_correct"] and res.record["official_correct"]
    assert [t["decision"] for t in res.record["trials"]] == ["B>A", "A>B"] and res.record["trials"][1]["decision_flipped"] == "B>A"
    assert gw.requests[0].generation.messages[0].text == jb.SYSTEM and all(r.call.role == role_enum("judge") for r in gw.requests)

    position_biased = await ev.run_item(item, judge_by(lambda a_right, n: "[[A>B]]"), candidate=CANDIDATE)
    assert position_biased.score == 0.0 and not position_biased.record["official_correct"]


async def test_one_right_and_one_tie_counts_only_under_official_scoring(jb_root: Path) -> None:
    rule = lambda a_right, n: "[[A=B]]" if n == 1 else ("[[A>B]]" if a_right else "[[B>A]]")  # noqa: E731
    item = jb.make(root=jb_root).items(1, 0)[0]
    strict = await jb.make(root=jb_root).run_item(item, judge_by(rule), candidate=CANDIDATE)
    assert strict.score == 0.0 and strict.record["official_correct"]
    official = await jb.make(root=jb_root, scoring="official").run_item(item, judge_by(rule), candidate=CANDIDATE)
    assert official.score == 1.0


async def test_a_missing_verdict_gets_one_follow_up(jb_root: Path) -> None:
    rule = lambda a_right, n: "Still thinking…" if n in (1, 3) else ("[[A>B]]" if a_right else "[[B>A]]")  # noqa: E731
    ev = jb.make(root=jb_root)
    gw = judge_by(rule)
    res = await ev.run_item(next(i for i in ev.items(5, 0) if i.id == "gpt:p1"), gw, candidate=CANDIDATE)
    assert res.score == 1.0 and len(gw.requests) == 4
    follow = gw.requests[1].generation.messages
    assert [m.role for m in follow][-2:] == [mg.CHAT_ROLE_ASSISTANT, mg.CHAT_ROLE_USER] and follow[-1].text == jb.CONTINUE


def test_judgebench_parsing_and_options(jb_root: Path) -> None:
    assert jb.parse_verdict("x [[A>>B]] y [[A>>B]]") == ("A>B", False)
    assert jb.parse_verdict("[[A>B]] then [[B>A]]") == (None, False) and jb.parse_verdict("none") == (None, True)
    assert jb.official_correct("A>B", "A>B", None) and not jb.official_correct("A>B", "A>B", "B>A")
    assert "[[A=B]]" not in jb.SYSTEM_NOTIE and "[[A=B]]" in jb.SYSTEM
    assert len(jb.make(root=jb_root, splits=("gpt", "claude")).rows()) == 3
    assert jb.make(splits=("claude",), allow_tie=False).version.endswith(":claude:notie")
    with pytest.raises(ValueError):
        jb.make(scoring="lenient")
