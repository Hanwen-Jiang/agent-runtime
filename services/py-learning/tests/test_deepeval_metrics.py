from agentruntime_learning.evals.deepeval_metrics import evaluate_cases, load_cases, score_case, validate_case


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
