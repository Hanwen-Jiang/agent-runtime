"""S-08 rubric 评分：用与被测模型不同家的模型打分。"""

from __future__ import annotations

import pytest
from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import enums_pb2, metering_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_learning.evals.cases import load_eval_set
from agentruntime_learning.evals.model import PlannerOutput
from agentruntime_learning.evals.rubric import GatewayRubricJudge, RubricScore, SameVendorError
from agentruntime_learning.evals.runner import EvalConfigError, run_eval

from support import ScriptedSubject, answer_stage, task_case, usage


class FakeGateway:
    def __init__(self, *, vendor: str = "vendor_b", score: float = 0.8, cost_micros: int = 1500) -> None:
        self.vendor = vendor
        self.score = score
        self.cost_micros = cost_micros
        self.requests: list[mg.GenerateRequest] = []

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        self.requests.append(request)
        result = struct_pb2.Value()
        json_format.ParseDict({"score": self.score, "notes": "按 rubric 打分"}, result)
        return mg.GenerateResponse(
            result=result,
            finish_reason=mg.FINISH_REASON_STOP,
            model=mg.ModelChoice(model_profile_id=f"{self.vendor}/judge", tier="strong@medium", vendor=self.vendor),
            usage=metering_pb2.Usage(cost_usd_micros=self.cost_micros),
        )

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        raise AssertionError("judge 不做嵌入")


CRITERIA = ["Stage 在信息检查点切开", "不可逆动作前有人工确认"]
MATERIAL = {"message": "订机票并付款", "stage": answer_stage()}


async def test_the_gateway_judge_asks_for_a_different_vendor() -> None:
    gateway = FakeGateway()
    judge = GatewayRubricJudge(gateway, tenant_id="evals", tier="strong@medium")
    score = await judge.score(criteria=CRITERIA, material=MATERIAL, subject_vendors=frozenset({"vendor_a"}))
    assert score == RubricScore(score=0.8, vendor="vendor_b", cost_usd=pytest.approx(0.0015))
    [request] = gateway.requests
    assert request.tenant.tenant_id == "evals"
    assert request.call.role == enums_pb2.ROLE_JUDGE
    assert request.call.avoid_vendor == "vendor_a"
    assert request.call.tier == "strong@medium"
    assert request.generation.output_schema.ByteSize() > 0, "judge 按结构化输出打分"
    prompt = "\n".join(m.text for m in request.generation.messages)
    assert all(c in prompt for c in CRITERIA)
    assert "answer@v1" in prompt, "被评的输出放在提示里"


async def test_a_judge_served_by_the_subjects_vendor_is_rejected() -> None:
    judge = GatewayRubricJudge(FakeGateway(vendor="vendor_a"), tenant_id="evals")
    with pytest.raises(SameVendorError):
        await judge.score(criteria=CRITERIA, material=MATERIAL, subject_vendors=frozenset({"vendor_a"}))


async def test_scores_outside_zero_to_one_are_rejected() -> None:
    judge = GatewayRubricJudge(FakeGateway(score=7), tenant_id="evals")
    with pytest.raises(ValueError):
        await judge.score(criteria=CRITERIA, material=MATERIAL, subject_vendors=frozenset({"vendor_a"}))


class FixedJudge:
    def __init__(self, vendor: str, score: float = 0.5) -> None:
        self.vendor = vendor
        self.value = score
        self.calls: list[dict] = []

    async def score(self, *, criteria, material, subject_vendors) -> RubricScore:
        self.calls.append({"criteria": criteria, "material": material, "subject_vendors": subject_vendors})
        if self.vendor in subject_vendors:
            raise SameVendorError(self.vendor)
        return RubricScore(score=self.value, vendor=self.vendor, cost_usd=0.002)


def subject_using(vendor: str) -> ScriptedSubject:
    return ScriptedSubject(plan_fn=lambda inp: PlannerOutput(triage_level="full", stage=answer_stage(), task_plan={"triage": {"level": "full"}, "horizon": "per_stage"}, usage=[usage(0.01, vendor=vendor)]))


async def test_the_runner_refuses_a_judge_of_the_same_declared_vendor(make_set) -> None:
    es = load_eval_set(make_set([task_case("t_one", rubric=CRITERIA)]))
    with pytest.raises(EvalConfigError):
        await run_eval(es, subject_using("vendor_a"), judge=FixedJudge("vendor_a"), judge_vendor="vendor_a", subject_vendor="vendor_a", reports_dir=None, compare=None)


async def test_the_runner_passes_the_subjects_vendors_to_the_judge(make_set) -> None:
    es = load_eval_set(make_set([task_case("t_one", rubric=CRITERIA)]))
    with pytest.raises(SameVendorError):
        await run_eval(es, subject_using("vendor_a"), judge=FixedJudge("vendor_a"), reports_dir=None, compare=None)


async def test_rubric_scores_are_reported_separately_from_pass_fail(make_set) -> None:
    es = load_eval_set(make_set([task_case("t_one", rubric=CRITERIA), task_case("t_two")]))
    judge = FixedJudge("vendor_b", score=0.25)
    report = await run_eval(es, subject_using("vendor_a"), judge=judge, reports_dir=None, compare=None)
    assert len(judge.calls) == 1, "只有带 rubric 的用例才打分"
    assert judge.calls[0]["criteria"] == CRITERIA
    assert report.data["metrics"]["rubric"] == {"scored": 1, "mean": pytest.approx(0.25)}
    assert report.data["metrics"]["success_rate"] == 1.0, "rubric 分数不影响通过与否"
    assert report.data["metrics"]["cost"]["judge_usd"] == pytest.approx(0.002)
    assert report.data["metrics"]["cost"]["total_usd"] == pytest.approx(0.02), "judge 的成本不算在被测对象头上"


async def test_without_a_judge_the_rubric_is_not_scored_rather_than_zero(make_set) -> None:
    es = load_eval_set(make_set([task_case("t_one", rubric=CRITERIA)]))
    report = await run_eval(es, subject_using("vendor_a"), reports_dir=None, compare=None)
    assert report.data["metrics"]["rubric"] == {"scored": 0, "mean": None}
    assert "未评" in report.summary()
