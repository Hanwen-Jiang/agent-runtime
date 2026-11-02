import json

from agentruntime_learning.evals import ragas


def _case(**overrides):
    value = {
        "case_id": "case-1",
        "split": "dev",
        "user_input": "where",
        "retrieved_contexts": ["context"],
        "reference": "answer",
        "response": "answer",
    }
    value.update(overrides)
    return value


def test_load_cases_accepts_wrapped_json(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": [_case()]}), encoding="utf-8")
    cases = ragas.load_cases(path)
    assert cases[0].case_id == "case-1"
    assert cases[0].retrieved_contexts == ["context"]


def test_missing_annotations_are_unscored(monkeypatch):
    monkeypatch.setattr(ragas, "_metric_classes", lambda: {})
    report = __import__("asyncio").run(ragas.run_cases([ragas.RagasCase(
        case_id="case-1", split="dev", user_input="where", retrieved_contexts=["context"]
    )]))
    assert report["cases"][0]["unscored"] == {
        "context_precision": "ragas_not_installed",
        "context_recall": "missing_reference",
        "faithfulness": "missing_response",
    }


def test_fixture_writes_report_and_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(ragas, "_metric_classes", lambda: {})
    source = tmp_path / "cases.json"
    source.write_text(json.dumps([_case()]), encoding="utf-8")
    out = tmp_path / "out"
    report = ragas.run_fixture(source, out=out)
    assert report["manifest"]["mode"] == "fixture"
    assert json.loads((out / "report.json").read_text())["eval"] == "ragas"
    assert json.loads((out / "manifest.json").read_text())["sample_count"] == 1
