"""SimpleQA 与 FRAMES（capability.evals.simpleqa、frames）：被测作答、Opus 裁判经 RecordingJudgeGateway 判分并留记录、同家冲突。"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_learning.capability.base import PinningGateway, role_enum
from agentruntime_learning.capability.evals import _judged, frames, simpleqa

from capability_datasets_support import ScriptGateway, text_of

OPUS = "a6api/claude-opus-5-5@2026-10"
GPT = "a6api/gpt-5.5@2026-10"
SONNET = "a6api/claude-sonnet-5-5@2026-10"


@pytest.fixture
def qa_csv(tmp_path: Path) -> Path:
    path = tmp_path / "simple_qa_test_set.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metadata", "problem", "answer"])
        w.writerow(["{'topic': 'Science and technology', 'answer_type': 'Person'}", "Who received the IEEE Frank Rosenblatt Award in 2010?", "Michio Sugeno"])
        w.writerow(["{'topic': 'Geography'}", "What is the capital of Australia?", "Canberra"])
    return path


@pytest.fixture
def frames_tsv(tmp_path: Path) -> Path:
    path = tmp_path / "test.tsv"
    cols = ["", "Prompt", "Answer"] + [f"wikipedia_link_{i}" for i in range(1, 11)] + ["wikipedia_link_11+", "reasoning_types", "wiki_links"]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(cols)
        w.writerow(["0", "If my future wife has the same first name as the 15th first lady of the United States' mother ...", "Jane Ballou",
                    "https://en.wikipedia.org/wiki/James_Buchanan", *[""] * 10, "Multiple constraints", "['https://en.wikipedia.org/wiki/James_Buchanan']"])
    return path


def scripted(answer: str, verdict: str, *, judge_served: str | None = None) -> ScriptGateway:
    def reply(req: mg.GenerateRequest):  # type: ignore[no-untyped-def]
        if req.call.role == role_enum("judge"):
            if judge_served:
                return mg.GenerateResponse(text=verdict, model=mg.ModelChoice(model_profile_id=judge_served))
            return verdict
        return answer

    return ScriptGateway(reply, cost_micros=3000)


async def test_simpleqa_answers_then_opus_grades_with_the_official_template(qa_csv: Path, tmp_path: Path) -> None:
    log = tmp_path / "run" / "judgments.jsonl"
    ev = simpleqa.make(source=qa_csv, judge_log=log)
    item = next(i for i in ev.items(5, 0) if "Rosenblatt" in i.payload["problem"])
    inner = scripted("Michio Sugeno received it.", "A")
    res = await ev.run_item(item, PinningGateway(inner, role="deliver", candidate=GPT), candidate=GPT)
    assert res.score == 1.0 and res.error is None and not res.contaminated
    answer_req, judge_req = inner.requests
    assert answer_req.call.role == role_enum("deliver") and answer_req.call.pinned_model_profile == GPT
    assert [m.text for m in answer_req.generation.messages] == [item.payload["problem"]], "题目原样作为唯一的用户消息"
    assert judge_req.call.role == role_enum("judge") and judge_req.call.pinned_model_profile == OPUS
    assert text_of(judge_req) == simpleqa.GRADER_TEMPLATE.format(question=item.payload["problem"], target="Michio Sugeno",
                                                                  predicted_answer="Michio Sugeno received it.")
    judge = res.record["judge"]
    assert judge["served"] == OPUS and judge["verdict"] == "CORRECT" and judge["reason"] == "A" and judge["cost_usd"] == pytest.approx(0.003)
    assert judge["messages"][0]["text"] == text_of(judge_req) and judge["temperature"] == "provider default"
    assert res.record["grade"] == "CORRECT" and res.record["same_vendor"] is False and res.cost_usd == pytest.approx(0.006)
    rows = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["eval"] == "simpleqa" and rows[0]["case_id"] == item.id and rows[0]["subject_models"] == [GPT]
    assert judge_req.call.tier == "strong@medium" and simpleqa.SimpleQAEval.uses_judge
    assert rows[0]["judge_pinned_ok"] and rows[0]["text"] == "A"


@pytest.mark.parametrize(("verdict", "grade"), [("B", "INCORRECT"), ("C", "NOT_ATTEMPTED"), ("unsure", "NOT_ATTEMPTED")])
async def test_simpleqa_only_correct_scores(qa_csv: Path, tmp_path: Path, verdict: str, grade: str) -> None:
    ev = simpleqa.make(source=qa_csv, judge_log=tmp_path / "j.jsonl")
    res = await ev.run_item(ev.items(1, 0)[0], scripted("I don't know.", verdict), candidate=GPT)
    assert res.score == 0.0 and res.record["grade"] == grade and res.error is None


async def test_same_vendor_is_flagged_and_an_unpinned_judge_is_an_error(qa_csv: Path, tmp_path: Path) -> None:
    ev = simpleqa.make(source=qa_csv, judge_log=tmp_path / "j.jsonl")
    res = await ev.run_item(ev.items(1, 0)[0], scripted("Canberra", "A"), candidate=SONNET)
    assert res.score == 1.0 and res.record["same_vendor"] is True
    rows = [json.loads(line) for line in (tmp_path / "j.jsonl").read_text("utf-8").splitlines()]
    assert rows[-1]["same_vendor"] is True

    moved = await ev.run_item(ev.items(1, 0)[0], scripted("Canberra", "A", judge_served=GPT), candidate=GPT)
    assert moved.score == 0.0 and moved.error and "JudgeNotPinned" in moved.error and moved.record["judge"]["served"] == GPT


async def test_a_failed_answer_is_an_error_before_judging(qa_csv: Path, tmp_path: Path) -> None:
    ev = simpleqa.make(source=qa_csv, judge_log=tmp_path / "j.jsonl")
    gw = ScriptGateway(lambda r: RuntimeError("provider down"))
    res = await ev.run_item(ev.items(1, 0)[0], gw, candidate=GPT)
    assert res.score == 0.0 and res.error == "RuntimeError: provider down" and len(gw.requests) == 1 and "judge" not in res.record


async def test_under_the_matrix_the_judgment_is_logged_once(qa_csv: Path, tmp_path: Path) -> None:
    from agentruntime_learning.evals.judging import RecordingJudgeGateway, judging

    own_log, matrix_log = tmp_path / "own.jsonl", tmp_path / "run" / "judgments.jsonl"
    ev = simpleqa.make(source=qa_csv, judge_log=own_log)
    gw = PinningGateway(RecordingJudgeGateway(scripted("Canberra", "A"), matrix_log), role="deliver", candidate=GPT)
    with judging(eval="simpleqa", case_id="x", repeat=0, candidate=GPT, subject_models=[GPT]):
        res = await ev.run_item(ev.items(1, 0)[0], gw, candidate=GPT)
    assert res.score == 1.0 and not own_log.exists() and len(matrix_log.read_text("utf-8").splitlines()) == 1
    assert json.loads(matrix_log.read_text("utf-8"))["metric"] == "simpleqa_grade" and res.record["judge"]["log"] == "matrix judgments.jsonl"


def test_simpleqa_template_and_data() -> None:
    assert hashlib.sha256(simpleqa.GRADER_TEMPLATE.encode()).hexdigest() == simpleqa.GRADER_SHA256
    assert simpleqa.GRADER_TEMPLATE.count("{") == 3 and simpleqa.parse_grade("B") == "B" and simpleqa.parse_grade("none") == "C"
    assert simpleqa.make().version.startswith("csv@") and simpleqa.make().role == "deliver"


async def test_frames_uses_the_paper_autorater(frames_tsv: Path, tmp_path: Path) -> None:
    ev = frames.make(source=frames_tsv, judge_log=tmp_path / "j.jsonl")
    [item] = ev.items(10, 0)
    assert item.id == "frames-0" and item.payload["reasoning_types"] == "Multiple constraints"
    inner = scripted("Her name would be Jane Ballou.", 'Explanation: The response names Jane Ballou.\nDecision: "TRUE"')
    res = await ev.run_item(item, inner, candidate=OPUS)
    assert res.score == 1.0 and res.record["same_vendor"] is True
    judge_req = inner.requests[1]
    assert judge_req.call.pinned_model_profile == OPUS and judge_req.generation.messages[0].text == "You are a helpful assistant."
    assert "- Predicted Answer: Her name would be Jane Ballou.\n- Ground Truth Answer: Jane Ballou" in judge_req.generation.messages[1].text
    assert res.record["judge"]["verdict"] == "TRUE" and res.record["judge"]["reason"] == "The response names Jane Ballou."

    false = await ev.run_item(item, scripted("Mary", "**Explanation:** wrong person.\n**Decision:** FALSE"), candidate=GPT)
    assert false.score == 0.0 and false.error is None
    unparsed = await ev.run_item(item, scripted("Mary", "Looks wrong to me."), candidate=GPT)
    assert unparsed.score == 0.0 and unparsed.error and "Decision" in unparsed.error


def test_frames_decision_parsing() -> None:
    assert frames.parse_decision('"Explanation:" x\n"Decision:" ("TRUE" or "FALSE")\nExplanation: y\nDecision: FALSE')[0] is False
    assert frames.parse_decision("decision: true")[0] is True and frames.parse_decision("no verdict")[0] is None
    assert _judged.same_vendor(SONNET) and not _judged.same_vendor(GPT)
