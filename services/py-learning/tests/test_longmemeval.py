"""docs/plan/memory-live.md 第 5 节（F2）：LongMemEval_S 测 Memory Service 的检索（agentruntime_learning.benchmarks.longmemeval）。

用几道很小的题离线跑（lexical 检索器，不联网）：读题、按轮切成记忆、分层抽题、每题单独的检索器、官方的检索指标、续跑、预算。
实现还没有时整个文件跳过。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

lme = pytest.importorskip("agentruntime_learning.benchmarks.longmemeval", reason="F2 的实现还没写：先审验收测试（docs/plan/memory-live.md 第 5 节）")


def _turn(role: str, content: str, has_answer: bool = False) -> dict[str, Any]:
    turn: dict[str, Any] = {"role": role, "content": content}
    if has_answer:
        turn["has_answer"] = True
    return turn


def _instance(qid: str, qtype: str, question: str, answer: str, sessions: list[tuple[str, str, list[dict[str, Any]]]], evidence: list[str]) -> dict[str, Any]:
    return {
        "question_id": qid, "question_type": qtype, "question": question, "answer": answer, "question_date": "2023/05/30 (Tue) 10:00",
        "haystack_session_ids": [s[0] for s in sessions], "haystack_dates": [s[1] for s in sessions],
        "haystack_sessions": [s[2] for s in sessions], "answer_session_ids": evidence,
    }


FILLER = [
    ("f1", "2023/05/01 (Mon) 09:00", [_turn("user", "Can you suggest a quick pasta recipe with tomatoes?"), _turn("assistant", "Try spaghetti with garlic, tomatoes and basil.")]),
    ("f2", "2023/05/02 (Tue) 09:00", [_turn("user", "Recommend a science fiction novel about space travel."), _turn("assistant", "Consider reading a classic about interstellar voyages.")]),
]

DOG = _instance("q-dog", "single-session-user", "What is the name of my dog?", "Max", [
    *FILLER,
    ("s-dog", "2023/05/20 (Sat) 02:21", [_turn("user", "My dog's name is Max, he is a beagle.", True), _turn("assistant", "Max sounds like a lovely beagle!"),
                                         _turn("user", "Any tips for walking him?"), _turn("assistant", "Keep walks regular and short at first.")]),
], ["s-dog"])

SPORTS = _instance("q-sports", "multi-session", "Which sports do I do every week, running and what else?", "Running and swimming", [
    ("s-run", "2023/05/10 (Wed) 08:00", [_turn("user", "I go running every Monday morning, about 5 km.", True), _turn("assistant", "Great habit!")]),
    *FILLER,
    ("s-swim", "2023/05/12 (Fri) 18:00", [_turn("user", "I also go swimming every Friday evening.", True), _turn("assistant", "Swimming is good for recovery.")]),
], ["s-run", "s-swim"])

CAT_ABS = _instance("q-cat_abs", "single-session-user", "What is the name of my cat?", "You did not mention a cat", [*FILLER], [])


def _write(path: Path, instances: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps(instances), "utf-8")
    return path


def _many() -> list[dict[str, Any]]:
    out = []
    for qtype in ("single-session-user", "multi-session", "temporal-reasoning"):
        for n in range(4):
            out.append(_instance(f"{qtype}-{n}", qtype, f"question {n}", "answer", [*FILLER], ["f1"]))
    return out


# 读题与切分。


def test_load_reads_the_fields_and_marks_abstention(tmp_path: Path) -> None:
    dog, cat = lme.load(_write(tmp_path / "s.json", [DOG, CAT_ABS]))
    assert dog.question_id == "q-dog" and dog.question_type == "single-session-user" and dog.answer == "Max"
    assert [s.id for s in dog.sessions] == ["f1", "f2", "s-dog"] and dog.sessions[2].date == "2023/05/20 (Sat) 02:21"
    assert dog.answer_session_ids == ("s-dog",) and not dog.abstention
    assert cat.abstention, "question_id 以 _abs 结尾的是拒答题：没有证据，不算检索指标"


def test_rounds_pair_a_user_turn_with_the_following_assistant_turn(tmp_path: Path) -> None:
    [dog] = lme.load(_write(tmp_path / "s.json", [DOG]))
    rounds = lme.rounds(dog)
    assert [r["id"] for r in rounds] == ["f1:0", "f2:0", "s-dog:0", "s-dog:1"]
    first = rounds[2]
    assert first["session"] == "s-dog" and first["has_answer"] is True and rounds[3]["has_answer"] is False
    assert first["text"].startswith("[2023/05/20 (Sat) 02:21]"), "带上会话日期：时间类的题要用"
    assert "user: My dog's name is Max" in first["text"] and "assistant: Max sounds like" in first["text"]
    assert all(r["kind"] for r in rounds)


def test_a_trailing_user_turn_is_its_own_round_and_empty_turns_are_dropped(tmp_path: Path) -> None:
    inst = _instance("q-x", "single-session-user", "q", "a", [("s", "2023/01/01 (Sun) 00:00", [
        _turn("user", "hello"), _turn("assistant", ""), _turn("user", "   "), _turn("user", "last words", True)])], ["s"])
    [x] = lme.load(_write(tmp_path / "s.json", [inst]))
    rounds = lme.rounds(x)
    assert [r["id"] for r in rounds] == ["s:0", "s:1"]
    assert "assistant:" not in rounds[0]["text"] and rounds[1]["has_answer"] is True


# 抽题：按题型分层、同一个种子同样的结果、拒答题不抽。


def test_sample_is_stratified_and_deterministic(tmp_path: Path) -> None:
    instances = lme.load(_write(tmp_path / "s.json", [*_many(), CAT_ABS]))
    picked = lme.sample(instances, limit=6, seed=0)
    assert sorted(i.question_type for i in picked) == sorted(["single-session-user"] * 2 + ["multi-session"] * 2 + ["temporal-reasoning"] * 2)
    assert [i.question_id for i in lme.sample(instances, limit=6, seed=0)] == [i.question_id for i in picked]
    assert all(not i.abstention for i in lme.sample(instances, limit=100, seed=1)), "拒答题没有证据会话，不进检索评测"


# 官方的检索指标（LongMemEval 的 recall_any、recall_all、ndcg_any；会话级由轮的排名去重得到）。


def test_session_ranking_keeps_the_first_occurrence() -> None:
    assert lme.session_ranking(["s2:0", "s1:3", "s2:1", "s3:0"]) == ["s2", "s1", "s3"]


def test_metrics_match_hand_computed_values() -> None:
    one = lme.metrics(["a", "b", "c"], {"b"}, ks=(1, 2, 3))
    assert one["recall_any@1"] == 0.0 and one["recall_any@2"] == 1.0
    assert one["ndcg_any@3"] == pytest.approx((1 / math.log2(3)) / 1.0)
    two = lme.metrics(["a", "b", "c"], {"a", "c"}, ks=(2, 3))
    assert two["recall_all@2"] == 0.0 and two["recall_all@3"] == 1.0
    assert two["ndcg_any@3"] == pytest.approx((1 + 1 / math.log2(4)) / (1 + 1 / math.log2(3)))


# 跑：每题单独的检索器（只看到这一题的轮），结果按题型汇总，续跑，预算。


async def test_run_with_the_lexical_retriever(tmp_path: Path) -> None:
    instances = lme.load(_write(tmp_path / "s.json", [DOG, SPORTS, CAT_ABS]))
    summary = await lme.run(instances, "lexical", tmp_path / "out", top_k=10, ks=(1, 5))
    rows = {r["question_id"]: r for r in map(json.loads, (tmp_path / "out" / "results.jsonl").read_text("utf-8").splitlines())}
    assert set(rows) == {"q-dog", "q-sports"}, "拒答题不跑"
    assert rows["q-dog"]["session"]["recall_any@1"] == 1.0 and rows["q-dog"]["ranked_sessions"][0] == "s-dog"
    assert rows["q-dog"]["turn"]["recall_any@1"] == 1.0, "轮级：has_answer 的那一轮排第一"
    assert rows["q-sports"]["session"]["recall_all@5"] == 1.0
    assert summary["questions"] == 2 and summary["skipped_abstention"] == 1
    assert set(summary["by_type"]) == {"single-session-user", "multi-session"}
    assert summary["session"]["recall_any@1"] == pytest.approx((1.0 + rows["q-sports"]["session"]["recall_any@1"]) / 2)
    assert summary["retriever"] == "lexical"
    assert json.loads((tmp_path / "out" / "summary.json").read_text("utf-8")) == summary


class Recording:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.closed = 0

    def factory(self, corpus: list[dict[str, str]]) -> Any:
        self.calls.append([c["id"] for c in corpus])
        outer = self

        class R:
            name = "recording"

            async def retrieve(self, question: str, top_k: int) -> list[dict[str, str]]:
                return [{"id": c["id"], "text": c["text"]} for c in corpus][:top_k]

            async def aclose(self) -> None:
                outer.closed += 1

        return R()


async def test_each_question_gets_its_own_retriever_over_its_own_rounds(tmp_path: Path) -> None:
    instances = lme.load(_write(tmp_path / "s.json", [DOG, SPORTS]))
    rec = Recording()
    await lme.run(instances, rec.factory, tmp_path / "out", top_k=5, ks=(5,))
    assert rec.calls == [[r["id"] for r in lme.rounds(i)] for i in instances]
    assert rec.closed == 2, "每题用完就关（Memory Service 的检索器在这里删掉写入的记忆）"


async def test_a_second_run_resumes(tmp_path: Path) -> None:
    instances = lme.load(_write(tmp_path / "s.json", [DOG, SPORTS]))
    first = Recording()
    await lme.run(instances[:1], first.factory, tmp_path / "out", top_k=5, ks=(5,))
    second = Recording()
    summary = await lme.run(instances, second.factory, tmp_path / "out", top_k=5, ks=(5,))
    assert len(second.calls) == 1, "做过的题不再跑"
    ids = [json.loads(line)["question_id"] for line in (tmp_path / "out" / "results.jsonl").read_text("utf-8").splitlines()]
    assert sorted(ids) == ["q-dog", "q-sports"] and summary["questions"] == 2


async def test_the_budget_is_checked_before_anything_runs(tmp_path: Path) -> None:
    instances = lme.load(_write(tmp_path / "s.json", [DOG, SPORTS]))
    estimate = lme.estimate_usd(instances, price_per_m=0.13)
    assert estimate > 0
    rec = Recording()
    with pytest.raises(lme.BudgetExceeded):
        await lme.run(instances, rec.factory, tmp_path / "out", top_k=5, ks=(5,), budget_usd=estimate / 2, price_per_m=0.13)
    assert rec.calls == [] and not (tmp_path / "out" / "results.jsonl").exists()


# 命令行。


def test_cli_fetch_run_and_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = _write(tmp_path / "source.json", [DOG, SPORTS, CAT_ABS])
    data = tmp_path / "longmemeval_s.json"
    assert lme.main(["fetch", "--url", source.as_uri(), "--out", str(data)]) == 0
    assert len(lme.load(data)) == 3
    assert lme.main(["run", "--data", str(data), "--out", str(tmp_path / "out"), "--retriever", "lexical", "--limit", "2", "--seed", "0"]) == 0
    assert json.loads((tmp_path / "out" / "summary.json").read_text("utf-8"))["questions"] == 2
    capsys.readouterr()
    assert lme.main(["report", "--out", str(tmp_path / "out")]) == 0
    assert "recall_any@" in capsys.readouterr().out


def test_fetch_rejects_a_file_that_is_not_longmemeval(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"id": 1}]), "utf-8")
    assert lme.main(["fetch", "--url", bad.as_uri(), "--out", str(tmp_path / "x.json")]) != 0
    assert not (tmp_path / "x.json").exists()
