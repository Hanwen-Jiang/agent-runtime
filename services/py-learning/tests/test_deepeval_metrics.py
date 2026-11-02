import asyncio
import importlib.util
from types import SimpleNamespace
from google.protobuf import json_format, struct_pb2

from agentruntime_learning.evals.deepeval_metrics import evaluate_cases, gateway_judge, score_case, validate_case


def test_fixture_metrics_and_unscored_semantics():
    case = {
        "case_id": "ok",
        "input": "fix it",
        "actual_output": "done",
        "expected_tools": [{"name": "repo_read"}],
        "tools_called": [{"name": "repo_read", "result_status": "ok"}],
        "task_completed": True,
        "rounds": 1,
    }
    result = score_case(case)
    assert result["ToolCorrectness"].score == 1.0
    assert result["TaskCompletion"].score == 1.0
    assert result["StepEfficiency"].score == 1.0
    missing = dict(case)
    missing.pop("expected_tools")
    assert score_case(missing)["ToolCorrectness"].score is None


def test_invalid_case_is_reported_without_aborting_batch():
    report = evaluate_cases([{"case_id": "bad", "input": "x"}])
    assert report["cases"][0]["status"] == "invalid"


def test_validate_case_rejects_wrong_types():
    assert "tools_called must be a list" in validate_case({"case_id": "x", "input": "x", "actual_output": "x", "tools_called": {}})


@__import__('unittest').skipUnless(importlib.util.find_spec('agentruntime') is not None, 'generated proto package unavailable')
def test_gateway_judge_uses_judge_role_and_budget():
    result = struct_pb2.Value()
    json_format.ParseDict({"score": 0.75, "reason": "acceptable"}, result)
    response = SimpleNamespace(result=result, model=SimpleNamespace(vendor="judge-vendor"), usage=SimpleNamespace(input_tokens=3, output_tokens=2, cost_usd_micros=1200))

    class Gateway:
        async def generate(self, request):
            assert request.call.role
            assert request.call.max_cost_usd_micros == 5000
            assert request.call.node_id == "deepeval_judge"
            return response

    out = asyncio.run(gateway_judge({"case_id": "x", "input": "q", "actual_output": "a", "tools_called": []}, Gateway(), tenant_id="t", max_cost_usd_micros=5000))
    assert out.score == 0.75
    assert out.cost_usd == 0.0012
