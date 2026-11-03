"""2026-11-03 人定：评测的裁判经 Model Gateway、钉 Opus 5.5、每次判分落盘（之后 gpt-6-astra 抽查复核）。

- RecordingJudgeGateway 只钉 role 为 judge 的调用，其他原样转发、不记；记录裁判看到的消息、输出 schema、回答、实际服务的模型、
  花费；没落在钉的模型上、出错都记下；被测一侧有同家模型时标 same_vendor。
- evals deepeval score 的网关路径与 evals ragas 的网关路径都经它判分（这里用假的框架对象，不依赖装好的 DeepEval、Ragas）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import enums_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

from agentruntime_learning.evals import deliverables as dv
from agentruntime_learning.evals import rag
from agentruntime_learning.evals.judging import DEFAULT_JUDGE, RecordingJudgeGateway, judging, summarize_log, vendor_of


class FakeGateway:
    def __init__(self, *, serve: str | None = None, fail: bool = False, result: dict[str, Any] | None = None, serve_first: list[str] | None = None) -> None:
        self.serve, self.fail, self.result = serve, fail, result
        self.serve_first = list(serve_first or [])
        self.seen: list[mg.GenerateRequest] = []

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        self.seen.append(request)
        if self.fail:
            raise RuntimeError("provider down")
        out = mg.GenerateResponse(text="judged")
        out.model.model_profile_id = (self.serve_first.pop(0) if self.serve_first else None) or self.serve or request.call.pinned_model_profile or "a6api/default@1"
        out.usage.input_tokens, out.usage.output_tokens, out.usage.cost_usd_micros = 120, 30, 1500
        if self.result is not None:
            value = struct_pb2.Value()
            json_format.ParseDict(self.result, value)
            out.result.CopyFrom(value)
        return out

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        raise NotImplementedError


def judge_request(text: str = "Is it right?") -> mg.GenerateRequest:
    r = mg.GenerateRequest()
    r.call.role = enums_pb2.ROLE_JUDGE
    r.generation.messages.add(role=mg.CHAT_ROLE_USER, text=text)
    return r


def rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


async def test_judge_calls_are_pinned_and_recorded(tmp_path: Path) -> None:
    inner = FakeGateway()
    gw = RecordingJudgeGateway(inner, tmp_path / "j.jsonl")
    with judging(eval="deepeval", case_id="c1", metric="correctness", subject_models=["a6api/claude-sonnet-5-5@2026-10"]):
        await gw.generate(judge_request("grade this"))
    other = mg.GenerateRequest()
    other.call.role = enums_pb2.ROLE_PLAN
    await gw.generate(other)
    assert inner.seen[0].call.pinned_model_profile == DEFAULT_JUDGE and inner.seen[1].call.pinned_model_profile == ""
    [row] = rows(tmp_path / "j.jsonl")
    assert (row["eval"], row["case_id"], row["metric"]) == ("deepeval", "c1", "correctness")
    assert row["messages"] == [{"role": "CHAT_ROLE_USER", "text": "grade this"}] and row["text"] == "judged"
    assert row["judge_served"] == DEFAULT_JUDGE and row["judge_pinned_ok"] and row["same_vendor"], "Sonnet 被测、Opus 裁判：同家"
    assert row["usage"]["cost_usd_micros"] == 1500 and gw.cost_usd_micros == 1500


async def test_misses_and_errors_are_recorded(tmp_path: Path) -> None:
    gw = RecordingJudgeGateway(FakeGateway(serve="a6api/other@1"), tmp_path / "j.jsonl", pin_attempts=2)
    await gw.generate(judge_request())
    bad = RecordingJudgeGateway(FakeGateway(fail=True), tmp_path / "j.jsonl")
    with pytest.raises(RuntimeError):
        await bad.generate(judge_request())
    summary = summarize_log(tmp_path / "j.jsonl")
    assert (summary["calls"], summary["retried"], summary["errors"], summary["not_pinned"]) == (3, 1, 1, 1)
    assert summary["cost_usd"] == 0.003, "被取代的那次也花了钱"


async def test_a_judgment_from_another_model_is_asked_again(tmp_path: Path) -> None:
    """裁判超时、Gateway 回退到别的模型：重问，直到落在钉的模型上；两次都记下，用的是钉中的那次。"""
    inner = FakeGateway(serve_first=["a6api/claude-sonnet-5-5@2026-10"])
    gw = RecordingJudgeGateway(inner, tmp_path / "j.jsonl")
    with judging(eval="ragas", case_id="q1", metric="llm_context_recall"):
        out = await gw.generate(judge_request())
    assert out.model.model_profile_id == DEFAULT_JUDGE and len(inner.seen) == 2
    first, second = rows(tmp_path / "j.jsonl")
    assert (first["attempt"], first["superseded"], first["judge_pinned_ok"]) == (1, True, False)
    assert (second["attempt"], second["judge_pinned_ok"], second["case_id"]) == (2, True, "q1") and "superseded" not in second
    summary = summarize_log(tmp_path / "j.jsonl")
    assert (summary["calls"], summary["retried"], summary["not_pinned"]) == (2, 1, 0)


def test_vendors() -> None:
    assert vendor_of(DEFAULT_JUDGE) == "anthropic" and vendor_of("a6api/gpt-6.1-sol@2026-11") == "openai"
    assert vendor_of("a6api/gemini-3.5-flash@2026-10") == "google"


class FakeMetric:
    """像 DeepEval 的指标：a_measure 时经模型（GatewayDeepEvalLLM）问一次裁判。"""

    def __init__(self, spec: dict[str, Any], model: Any) -> None:
        self.spec, self.model = spec, model

    async def a_measure(self, tc: Any) -> None:
        answer = await self.model.a_generate(f"{self.spec['name']}: {tc.actual_output}")
        self.score, self.success, self.reason = (0.9, True, answer) if "inventory" in tc.actual_output else (0.1, False, answer)


class FakeCase:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


async def test_deliverables_gateway_scoring_records_every_judgment(tmp_path: Path) -> None:
    cases = dv.load_cases()[:2]
    todo = [{"id": c.instance_id, "input": c.message, "actual_output": "inventory/core.py" if i == 0 else "no idea", "expected_output": c.expected_output,
             "metrics": [{"name": m, **dv.METRICS[m]} for m in c.metrics]} for i, c in enumerate(cases)]
    inner = FakeGateway()
    out = await dv.gateway_scores(todo, inner, tmp_path / "judgments.jsonl", subject_models=["a6api/gpt-6.1-sol@2026-11"], metric_factory=FakeMetric, test_case=FakeCase)
    assert out[0]["scores"]["correctness"]["success"] is True and out[1]["scores"]["correctness"]["success"] is False
    logged = rows(tmp_path / "judgments.jsonl")
    assert len(logged) == sum(len(c.metrics) for c in cases), "每个指标一次裁判调用，全部记下"
    assert {r["case_id"] for r in logged} == {c.instance_id for c in cases} and not any(r["same_vendor"] for r in logged)
    assert all(r["judge_served"] == DEFAULT_JUDGE for r in logged)


class FakeRagasMetric:
    def __init__(self, llm: Any) -> None:
        self.llm = llm

    async def single_turn_ascore(self, sample: Any) -> float:
        await self.llm.agenerate("judge the contexts")
        return 0.75


async def test_rag_gateway_metrics_go_through_the_recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentruntime_learning.evals import ragas as rg

    monkeypatch.setattr(rg, "_metric_classes", lambda: {"context_precision": FakeRagasMetric, "context_recall": FakeRagasMetric})
    corpus, questions = rag.load_set()
    summary = await rag.run(rag.LexicalRetriever(corpus), corpus, questions[:2], tmp_path, judge_gateway=FakeGateway(result={}))
    assert summary["ragas"] == "gateway" and summary["judge_model"] == DEFAULT_JUDGE
    assert summary["metrics"]["llm_context_precision"] == 0.75 and summary["metrics"]["llm_context_recall"] == 0.75
    assert len(rows(tmp_path / "judgments.jsonl")) == 4


async def test_rag_gateway_without_ragas_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentruntime_learning.evals import ragas as rg

    monkeypatch.setattr(rg, "_metric_classes", lambda: {})
    corpus, questions = rag.load_set()
    await rag.run(rag.LexicalRetriever(corpus), corpus, questions[:1], tmp_path, judge_gateway=FakeGateway())
    [row] = [json.loads(line) for line in (tmp_path / "results.jsonl").read_text("utf-8").splitlines()]
    assert row["ragas_errors"] == {"llm_context_precision": "ragas_not_installed", "llm_context_recall": "ragas_not_installed"}
