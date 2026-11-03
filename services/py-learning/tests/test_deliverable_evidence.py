"""EV-DE: pass bounded, observed tool events to honesty scoring, without inventing a full trace."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from agentruntime_learning.benchmarks import swebench as sb
from agentruntime_learning.evals import deliverables as dv
from swebench_support import RUN, FakeRuntime, Script, settings


class DlvRuntime(FakeRuntime):
    def __init__(self, scripts: dict[str, Script]) -> None:
        super().__init__({})
        self.scripts = {dv.workspace_id(RUN, cid): script for cid, script in scripts.items()}


def tool(state: str, *, step: int = 1, **fields: Any) -> tuple[str, dict[str, Any]]:
    return "tool_status", {"round_seq": 1, "node_id": "inspect", "step": step, "tool": "repo_read", "state": state,
                           "target": "inventory/core.py", **fields}


def run_case(tmp_path: Path, script: Script) -> tuple[dict[str, Any], DlvRuntime]:
    case = dv.load_cases()[0]
    runtime = DlvRuntime({case.instance_id: script})
    assert dv.run(runtime, [case], tmp_path, settings()).finished == 1
    return sb.read_records(tmp_path)[case.instance_id], runtime


def test_run_saves_only_observed_tool_events_once_across_reconnect(tmp_path: Path) -> None:
    rec, runtime = run_case(tmp_path, Script(events=[
        ("task_accepted", {}), tool("running"), tool("failed"),
        ("node_finished", {"result": "not tool evidence"}), tool("succeeded", step=2),
    ], drop_after=2, overlap=True, deliverable="I read inventory/core.py."))
    assert len(runtime.event_calls) == 2
    evidence = rec["tool_evidence"]
    assert evidence["truncated"] is False
    assert [event["event_id"] for event in evidence["events"]] == [2, 3, 5]
    assert [event["state"] for event in evidence["events"]] == ["running", "failed", "succeeded"]
    assert evidence["events"][0] == {
        "event_id": 2, "round_seq": 1, "node_id": "inspect", "step": 1,
        "tool": "repo_read", "state": "running", "target": "inventory/core.py",
    }


def test_evidence_is_bounded_redacted_and_excludes_arguments_and_output(tmp_path: Path) -> None:
    assert dv.TOOL_EVIDENCE_EVENTS == 100
    rec, _ = run_case(tmp_path, Script(events=[
        tool("running", target="curl -H 'Authorization: Bearer secretvalue123' https://example.test/?token=hiddenvalue456",
             args={"password": "private-argument"}, output="private-output", error="private-error"),
        tool("failed", target="x" * 5000),
        *[tool("succeeded", step=n) for n in range(dv.TOOL_EVIDENCE_EVENTS)],
    ]))
    evidence = rec["tool_evidence"]
    assert len(evidence["events"]) == 100 and evidence["truncated"] is True
    text = json.dumps(evidence)
    for secret in ("secretvalue123", "hiddenvalue456", "private-argument", "private-output", "private-error"):
        assert secret not in text
    assert len(evidence["events"][1]["target"]) < 5000
    allowed = {"event_id", "round_seq", "node_id", "step", "tool", "state", "target"}
    assert all(set(event) <= allowed for event in evidence["events"])


def test_follow_rebuilds_evidence_instead_of_appending_a_previous_attempt(tmp_path: Path) -> None:
    case = dv.load_cases()[0]
    runtime = DlvRuntime({case.instance_id: Script(events=[tool("running")])})
    sub = runtime.submit(workspace_id=dv.workspace_id(RUN, case.instance_id), message=case.message, client_request_id="one")
    rec = {**sub, "tool_evidence": {"events": [{"event_id": 999, "target": "stale previous task"}], "truncated": True}}
    runner = dv._Runner(runtime, [case], tmp_path, settings())
    runner._follow(rec, interactive=False)
    assert rec["tool_evidence"]["truncated"] is False
    assert [event["event_id"] for event in rec["tool_evidence"]["events"]] == [1]
    first = json.dumps(rec["tool_evidence"])
    runner._follow(rec, interactive=False)
    assert json.dumps(rec["tool_evidence"]) == first, "rereading the same Task must not append duplicate evidence"


@pytest.mark.parametrize("evidence, marker", [
    (None, "unavailable"),
    ({"events": [], "truncated": False}, "no"),
    ({"events": [{"event_id": 2, "state": "failed", "tool": "shell_run", "target": "pytest"}], "truncated": True}, "truncat"),
])
def test_bridge_cases_identify_missing_empty_or_truncated_observations(evidence: Any, marker: str) -> None:
    case = dv.load_cases()[0]
    rec = {"state": "finished", "deliverable": "I could not run the tests."}
    if evidence is not None:
        rec["tool_evidence"] = evidence
    [row] = dv.bridge_cases([case], {case.instance_id: rec})
    assert row["input"] == case.message and row["actual_output"] == rec["deliverable"]
    observations = row["tool_evidence"]
    assert isinstance(observations, list) and observations and all(isinstance(item, str) for item in observations)
    notice = observations[0].lower()
    assert marker in notice
    assert "complete" in notice or "limited" in notice, "SSE observations must not be presented as a full execution trace"
    if evidence and evidence["events"]:
        assert json.loads(observations[1])["state"] == "failed"
    else:
        assert len(observations) == 1, "missing evidence must not generate synthetic tool calls"
    metrics = {metric["name"]: metric for metric in row["metrics"]}
    assert "tool_evidence" in metrics["honesty"]["params"]
    assert all("tool_evidence" not in metric.get("params", []) for name, metric in metrics.items() if name != "honesty")


def test_honesty_does_not_punish_admitting_an_earlier_error() -> None:
    criteria = dv.METRICS["honesty"]["criteria"].lower()
    assert "must not confess" not in criteria
    assert "correct" in criteria or "admit" in criteria
    assert "running" in criteria and "failed" in criteria, "attempts and failures must not count as successful operations"


async def test_gateway_and_bridge_keep_tool_evidence_separate_from_retrieval_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Both scoring paths use the same DeepEval field contract; no model, network, or optional install is needed.
    seen: list[tuple[dict[str, Any], Any]] = []

    class Metric:
        def __init__(self, **kw: Any) -> None:
            self.kw = kw
            self.score, self.success, self.reason = 1.0, True, "transport verified"

        def measure(self, case: Any) -> None:
            seen.append((self.kw, case))

        async def a_measure(self, case: Any) -> None:
            self.measure(case)

    class BaseLLM:
        def __init__(self, **kw: Any) -> None:
            pass

    params = SimpleNamespace(INPUT="input", ACTUAL_OUTPUT="actual_output", EXPECTED_OUTPUT="expected_output",
                             CONTEXT="context", RETRIEVAL_CONTEXT="retrieval_context")
    modules = {
        "deepeval": {},
        "deepeval.metrics": {"AnswerRelevancyMetric": Metric, "FaithfulnessMetric": Metric, "GEval": Metric},
        "deepeval.models": {"DeepEvalBaseLLM": BaseLLM},
        "deepeval.test_case": {"LLMTestCase": SimpleNamespace, "SingleTurnParams": params, "LLMTestCaseParams": params},
    }
    for name, attrs in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)

    path = Path(dv.__file__).parent / "bridges" / "deepeval_bridge.py"
    spec = importlib.util.spec_from_file_location("ev_de_test_bridge", path)
    assert spec and spec.loader
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    row = {"id": "evidence", "input": "Which files did you read?", "actual_output": "inventory/core.py",
           "tool_evidence": ["Limited observed tool events.", '{"state":"succeeded","target":"inventory/core.py"}'],
           "context": ["separate retrieved reference"], "metrics": [{"name": "honesty", **dv.METRICS["honesty"]}]}
    gateway = await dv.gateway_scores([row], object(), tmp_path / "judgments.jsonl", test_case=SimpleNamespace)
    direct = bridge.score_case(row, object())
    assert gateway == [direct] and len(seen) == 2
    for metric, case in seen:
        assert metric["evaluation_params"] == ["input", "actual_output", "context"]
        assert case.context == row["tool_evidence"]
        assert case.retrieval_context == row["context"] and case.input == row["input"]
    retrieval_spec = {"name": "retrieval", "kind": "geval", "criteria": "Compare retrieved facts", "params": ["context"]}
    assert dv._deepeval_metric(retrieval_spec, object()).kw["evaluation_params"] == ["retrieval_context"]
    assert bridge.build_metric(retrieval_spec, object()).kw["evaluation_params"] == ["retrieval_context"]
