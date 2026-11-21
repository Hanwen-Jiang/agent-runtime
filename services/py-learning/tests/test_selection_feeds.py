import json

from agentruntime_learning.evals.dashboard_server import _clean_report_document, _with_benchmark_selections, combine


def test_live_selection_counts_unique_cases_not_strategy_executions():
    labels = {"eval_run_id": "ctx", "eval_suite": "context", "eval_subject": "worker", "eval_split": "dev"}
    historical = {"metric": labels, "value": [0, "10"]}
    assert "selection_info" not in combine({"planned": [historical]}, 0)[0]
    declared = {"metric": {**labels, "eval_selection_method": "predefined_split", "eval_selection_dataset": "context",
                           "eval_selection_population": "7", "eval_selection_count": "5", "eval_selection_seed": "0"},
                "value": [0, "10"]}
    row, = combine({"planned": [declared]}, 0)
    assert row["planned"] == 10
    assert row["selection_info"] == {"dataset": "context", "method": "predefined_split", "split": "dev",
                                      "source": "run_telemetry", "population_count": 7, "selected_count": 5, "seed": 0}


def test_archive_joins_only_matching_public_run_and_does_not_unseal_a_split():
    info = {"method": "explicit_ids", "selected_count": 1, "selection_ids": ["django__django-1"], "split": "test"}
    manifests = {"runs": [{"suite": "swebench", "run_id": "abc", "selection_info": info}]}
    rows = [{"run_id": "abc", "selected": 1}, {"run_id": "other", "selected": 1}, {"run_id": "abc", "selected": 2},
            {"run_id": "abc", "selected": 1, "selection_info": {"split": "held-out"}}]
    body = json.dumps({"reports": {"tuning": rows}}).encode()
    merged = json.loads(_with_benchmark_selections(body, manifests, archive=True))["reports"]["tuning"]
    assert merged[0]["selection_info"] == info
    assert merged[1:] == rows[1:]


def test_only_safe_metadata_is_read_from_legacy_archives():
    private_id = "django__django-123"
    row = {"split": "holdout", "selection_info": {"dataset": "test", "method": "predefined_split", "source": "/internal/path",
           "method_detail_en": "not a public source", "selected_count": 1, "selection_ids": [private_id],
           "groups": [{"selection_ids": [private_id], "selected_count": 1, "split": "test"}]}}
    clean = _clean_report_document({"reports": {"runs": [row]}})["reports"]["runs"][0]["selection_info"]
    assert clean["selected_count"] == 1 and clean["method"] == "predefined_split"
    assert "source" not in clean and "method_detail_en" not in clean and private_id not in json.dumps(clean)
