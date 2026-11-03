"""docs/plan/feedback-2026-11-01.md N-12、N-13：DeepEval 与 Ragas 经桥接入（agentruntime_learning.evals.frameworks、deliverables、rag）。

- 桥只 import 标准库与框架本身（在单独的 Python 里用 -I 跑，不读本仓库的代码）。
- 判分模型的配置：AR_JUDGE_* 优先，其次 AR_MODEL_PROVIDER_*，再其次 model-provider.env；要给模型；密钥不进 repr 与报错。
- 给桥的环境只有白名单里的变量与判分模型的几个；桥失败时报错里的密钥换成掩码；少给了结果是错。
- 交付质量（DeepEval）：用例与 fixture 的校验；经 Task API 跑（工作区从 fixture 的文件建，没有远端），记下交付；打分只打跑完、
  没打过的；汇总与通过率；报告写明没确认的标注。
- 记忆检索（Ragas）：评测集校验；lexical 基线；hit、recall、MRR；没给框架的 Python 时不跑桥，给了没给判分模型时只算离线指标。
- 设 AR_TEST_FRAMEWORKS_PYTHON（装了 eval-requirements.txt 的 Python）时对真的 Ragas 跑离线指标、对真的 DeepEval 跑一条 G-Eval
  （判分模型是本机的假接口）。
"""

from __future__ import annotations

import ast
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agentruntime_learning.benchmarks import swebench as sb
from agentruntime_learning.evals import deliverables as dv
from agentruntime_learning.evals import frameworks as fw
from agentruntime_learning.evals import rag

from swebench_support import FakeRuntime, Script, settings
from test_swebench_telemetry import Recorder

KEY = "sk-judge-test-0123456789"
STDLIB = set(sys.stdlib_module_names)


# --- 桥 ---


@pytest.mark.parametrize(("bridge", "allowed"), [("deepeval", {"deepeval"}), ("ragas", {"ragas", "langchain_openai"})])
def test_bridges_import_only_the_stdlib_and_their_framework(bridge: str, allowed: set[str]) -> None:
    tree = ast.parse((fw.BRIDGES / f"{bridge}_bridge.py").read_text("utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    assert roots - STDLIB - {"__future__"} <= allowed, roots - STDLIB
    assert not any(r.startswith("agentruntime") for r in roots), "桥不 import 本仓库的代码"


def test_the_framework_versions_are_pinned() -> None:
    lines = [ln for ln in fw.REQUIREMENTS.read_text("utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    assert {ln.split("==")[0] for ln in lines} >= {"deepeval", "ragas", "langchain-openai", "rapidfuzz"} and all("==" in ln for ln in lines)


# --- 判分模型的配置 ---


def test_judge_config_prefers_the_judge_variables(tmp_path: Path) -> None:
    env = {"AR_JUDGE_BASE_URL": "https://judge.example/v1", "AR_JUDGE_API_KEY": KEY, "AR_MODEL_PROVIDER_BASE_URL": "https://relay.example/v1", "AR_MODEL_PROVIDER_API_KEY": "other"}
    cfg = fw.judge_config("gpt-x", env=env, provider_file=tmp_path / "none.env")
    assert (cfg.base_url, cfg.api_key, cfg.model) == ("https://judge.example/v1", KEY, "gpt-x")
    assert KEY not in repr(cfg)
    relay = fw.judge_config("gpt-x", env={"AR_MODEL_PROVIDER_BASE_URL": "https://relay.example/v1", "AR_MODEL_PROVIDER_API_KEY": KEY}, provider_file=tmp_path / "none.env")
    assert relay.base_url == "https://relay.example/v1"
    file = tmp_path / "model-provider.env"
    file.write_text(f'AR_MODEL_PROVIDER_BASE_URL="https://file.example/v1"\nexport AR_MODEL_PROVIDER_API_KEY={KEY}\n', "utf-8")
    assert fw.judge_config("m", env={}, provider_file=file).base_url == "https://file.example/v1"


@pytest.mark.parametrize("env", [{}, {"AR_JUDGE_BASE_URL": "http://judge.example/v1", "AR_JUDGE_API_KEY": KEY}])
def test_judge_config_errors_do_not_leak_the_key(env: dict[str, str], tmp_path: Path) -> None:
    with pytest.raises(ValueError) as err:
        fw.judge_config("m", env=env, provider_file=tmp_path / "none.env")
    assert KEY not in str(err.value)
    with pytest.raises(ValueError):
        fw.judge_config("", env={"AR_JUDGE_BASE_URL": "https://x", "AR_JUDGE_API_KEY": KEY})


# --- run_bridge（假的桥） ---

FAKE_BRIDGE = r'''
import argparse, json, os, sys
p = argparse.ArgumentParser(); p.add_argument("--cases"); p.add_argument("--out"); p.add_argument("--offline", action="store_true"); p.add_argument("--fail", action="store_true")
a, _ = p.parse_known_args()
rows = [json.loads(l) for l in open(a.cases, encoding="utf-8") if l.strip()]
if os.environ.get("FAKE_FAIL"):
    print("boom with key " + os.environ.get("AR_JUDGE_API_KEY", ""), file=sys.stderr); sys.exit(3)
seen = {"env": sorted(k for k in os.environ if k.startswith(("AR_", "FAKE_"))), "offline": a.offline}
with open(a.out, "w", encoding="utf-8") as f:
    for r in rows[: int(os.environ.get("FAKE_LIMIT", len(rows)))]:
        f.write(json.dumps({"id": r["id"], "seen": seen, "scores": {"x": 1.0}}) + "\n")
'''


@pytest.fixture
def bridges(tmp_path: Path) -> Path:
    d = tmp_path / "bridges"
    d.mkdir()
    (d / "fake_bridge.py").write_text(FAKE_BRIDGE, "utf-8")
    return d


def judge() -> fw.JudgeConfig:
    return fw.JudgeConfig("https://judge.example/v1", KEY, "gpt-x")


def test_the_bridge_gets_only_the_judge_variables(bridges: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AR_TASK_API_TOKEN", "task-token-should-not-pass")
    rows = fw.run_bridge("fake", sys.executable, [{"id": "a"}, {"id": "b"}], judge=judge(), bridges=bridges, args=("--offline",))
    assert [r["id"] for r in rows] == ["a", "b"]
    assert rows[0]["seen"]["env"] == ["AR_JUDGE_API_KEY", "AR_JUDGE_BASE_URL", "AR_JUDGE_MODEL"] and rows[0]["seen"]["offline"] is True


def test_a_failing_bridge_reports_without_the_key(bridges: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_FAIL", "1")
    monkeypatch.setattr(fw, "PASS_THROUGH", (*fw.PASS_THROUGH, "FAKE_FAIL"))
    with pytest.raises(fw.BridgeError) as err:
        fw.run_bridge("fake", sys.executable, [{"id": "a"}], judge=judge(), bridges=bridges)
    assert "boom" in str(err.value) and KEY not in str(err.value) and "***" in str(err.value)


def test_missing_results_are_an_error(bridges: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_LIMIT", "1")
    monkeypatch.setattr(fw, "PASS_THROUGH", (*fw.PASS_THROUGH, "FAKE_LIMIT"))
    with pytest.raises(fw.BridgeError, match="少给了 1 条"):
        fw.run_bridge("fake", sys.executable, [{"id": "a"}, {"id": "b"}], bridges=bridges)


def test_a_missing_interpreter_says_how_to_build_one(bridges: Path) -> None:
    with pytest.raises(fw.BridgeError, match="eval-requirements.txt"):
        fw.run_bridge("fake", "/nonexistent/python", [{"id": "a"}], bridges=bridges)


# --- 交付质量（DeepEval） ---


def test_the_deliverables_set_is_valid() -> None:
    cases = dv.load_cases()
    assert len(cases) >= 8 and all(not c.confirmed for c in cases), "起草的标注，要经人确认"
    assert {"honesty", "correctness", "relevancy"} <= {m for c in cases for m in c.metrics}
    files = {f["path"] for f in dv.fixture_files()}
    assert {"README.md", "inventory/core.py", "data/stock.csv", "tests/test_core.py"} <= files


def test_bad_cases_are_refused(tmp_path: Path) -> None:
    (tmp_path / "cases.jsonl").write_text(json.dumps({"id": "a", "message": "m", "metrics": ["correctness"]}) + "\n", "utf-8")
    with pytest.raises(ValueError, match="expected_output"):
        dv.load_cases(tmp_path)
    (tmp_path / "cases.jsonl").write_text(json.dumps({"id": "a", "message": "m", "metrics": ["vibes"]}) + "\n", "utf-8")
    with pytest.raises(ValueError, match="vibes"):
        dv.load_cases(tmp_path)


class DlvRuntime(FakeRuntime):
    def __init__(self, scripts: dict[str, Script]) -> None:
        from swebench_support import RUN

        super().__init__({})
        self.scripts = {dv.workspace_id(RUN, cid): s for cid, s in scripts.items()}


def test_deliverables_run_records_the_reply(tmp_path: Path) -> None:
    cases = dv.load_cases()[:2]
    fake = DlvRuntime({cases[0].instance_id: Script(deliverable={"value": {"answer": "根目录有 inventory/ …"}}, cost_usd=0.05),
                       cases[1].instance_id: Script(deliverable="0.3.1, in pyproject.toml and inventory/__init__.py", cost_usd=0.02)})
    live = Recorder()
    summary = dv.run(fake, cases, tmp_path / "out", settings(model_name="sol"), live=live)
    assert summary.finished == 2
    created = next(iter(fake.created.values()))
    assert (created["repo_url"], created["ref"]) == ("", "") and {f["path"] for f in created["files"]} == {f["path"] for f in dv.fixture_files()}
    assert [s["message"] for s in fake.submits] == [c.message for c in cases]
    recs = sb.read_records(tmp_path / "out")
    assert recs[cases[0].instance_id]["deliverable"] == "根目录有 inventory/ …" and recs[cases[1].instance_id]["deliverable"].startswith("0.3.1")
    assert live.of("finished") == ["sol"]


def fake_scores(name: str, python: str, cases: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
    assert name == "deepeval" and kw["judge"].model == "gpt-x"
    out = []
    for c in cases:
        ok = "inventory" in c["actual_output"] or "0.3.1" in c["actual_output"]
        out.append({"id": c["id"], "scores": {m["name"]: {"score": 0.9 if ok else 0.2, "success": ok, "reason": "r"} for m in c["metrics"]}})
    return out


def test_scoring_covers_finished_unscored_cases_and_summarizes(tmp_path: Path) -> None:
    cases = dv.load_cases()[:3]
    fake = DlvRuntime({cases[0].instance_id: Script(deliverable="inventory/core.py …"), cases[1].instance_id: Script(deliverable="I have no tools."),
                       cases[2].instance_id: Script(deliverable="243")})
    out = tmp_path / "out"
    dv.run(fake, cases, out, settings())
    calls: list[int] = []

    def bridge(name: str, python: str, rows: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
        calls.append(len(rows))
        assert all(r["metrics"] and r["input"] for r in rows)
        assert rows[0]["metrics"][0]["kind"] == "geval" and rows[0]["expected_output"]
        return fake_scores(name, python, rows, **kw)

    live = Recorder()
    summary = dv.score(out, python="py", judge=judge(), live=live, bridge=bridge)
    assert calls == [3]
    assert summary["selected"] == 3 and summary["passed_all"] == 1 and summary["pass_rate"] == pytest.approx(1 / 3)
    assert summary["metrics"]["correctness"]["passed"] == 1
    assert dict(live.of("score"))["pass_rate"] == pytest.approx(1 / 3)
    dv.score(out, python="py", judge=judge(), bridge=bridge)
    assert calls == [3], "打过分的不重打"
    rep = dv.report(out)
    assert rep["unconfirmed"] and "没经人确认" in (out / "report.md").read_text("utf-8")
    from agentruntime_learning.evals.__main__ import main
    assert main(["deepeval", "report", "--out", str(out)]) == 0, "report 子命令没有 --eval-set（2026-11-03 t18 里报 AttributeError）"


# --- 记忆检索（Ragas） ---


def test_the_memory_rag_set_is_valid() -> None:
    corpus, questions = rag.load_set()
    assert len(corpus) >= 16 and len(questions) >= 10 and all(not q.confirmed for q in questions)


def test_the_set_is_checked(tmp_path: Path) -> None:
    (tmp_path / "corpus.jsonl").write_text(json.dumps({"id": "m1", "text": "a"}) + "\n", "utf-8")
    (tmp_path / "questions.jsonl").write_text(json.dumps({"id": "q", "question": "?", "reference_ids": ["m9"]}) + "\n", "utf-8")
    with pytest.raises(ValueError, match="m9"):
        rag.load_set(tmp_path)


def test_retrieval_metrics() -> None:
    assert rag.retrieval_metrics(["b", "a", "c"], ["a"]) == {"hit": 1.0, "recall": 1.0, "mrr": 0.5}
    assert rag.retrieval_metrics(["b"], ["a", "c"]) == {"hit": 0.0, "recall": 0.0, "mrr": 0.0}
    assert rag.retrieval_metrics(["a", "x"], ["a", "c"])["recall"] == 0.5


async def test_the_lexical_baseline_finds_the_obvious_one() -> None:
    corpus, _ = rag.load_set()
    got = await rag.LexicalRetriever(corpus).retrieve("payments service port health check", 2)
    assert got[0]["id"] == "m02"


class AsyncRecorder(Recorder):
    async def planned(self, total: int, **kw: Any) -> None:  # type: ignore[override]
        super().planned(total, **kw)

    async def case(self, outcome: str, cost_usd: float | None = None) -> None:  # type: ignore[override]
        super().case(outcome, cost_usd)

    async def score(self, metric: str, value: float) -> None:  # type: ignore[override]
        super().score(metric, value)

    async def finished(self, *, model: str = "") -> None:  # type: ignore[override]
        super().finished(model=model)


async def test_a_rag_run_scores_retrieval_and_calls_ragas_offline_without_a_judge(tmp_path: Path) -> None:
    corpus, questions = rag.load_set()
    seen: dict[str, Any] = {}

    def bridge(name: str, python: str, rows: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
        seen.update(name=name, args=kw.get("args"), judge=kw.get("judge"), first=rows[0])
        return [{"id": r["id"], "scores": {"non_llm_context_recall": 1.0}} for r in rows]

    live = AsyncRecorder()
    summary = await rag.run(rag.LexicalRetriever(corpus), corpus, questions, tmp_path, python="py", live=live, bridge=bridge)
    assert seen["name"] == "ragas" and seen["args"] == ("--offline",) and seen["judge"] is None
    assert seen["first"]["reference_contexts"] and seen["first"]["retrieved_contexts"] and set(seen["first"]) >= {"id", "user_input", "reference"}
    assert summary["ragas"] == "offline" and summary["metrics"]["non_llm_context_recall"] == 1.0 and 0 < summary["metrics"]["hit"] <= 1
    assert live.calls[0] == ("planned", (len(questions), 0, 0.0)) and len(live.of("case")) == len(questions)
    rows = [json.loads(line) for line in (tmp_path / "results.jsonl").read_text("utf-8").splitlines()]
    assert len(rows) == len(questions) and "_bridge" not in rows[0]


async def test_without_a_framework_python_ragas_is_skipped(tmp_path: Path) -> None:
    corpus, questions = rag.load_set()
    summary = await rag.run(rag.LexicalRetriever(corpus), corpus, questions[:2], tmp_path)
    assert summary["ragas"] == "skipped" and set(summary["metrics"]) == {"hit", "recall", "mrr"}


def test_a_retriever_can_be_loaded_by_name() -> None:
    corpus, _ = rag.load_set()
    assert isinstance(rag.load_retriever("lexical", corpus), rag.LexicalRetriever)
    assert type(rag.load_retriever("agentruntime_learning.evals.rag:LexicalRetriever", corpus)).__name__ == "LexicalRetriever"
    with pytest.raises(ValueError):
        rag.load_retriever("bm25", corpus)


# --- 真的框架（设 AR_TEST_FRAMEWORKS_PYTHON 才跑） ---

FRAMEWORKS = os.environ.get("AR_TEST_FRAMEWORKS_PYTHON", "")
needs_frameworks = pytest.mark.skipif(not FRAMEWORKS, reason="设 AR_TEST_FRAMEWORKS_PYTHON（装了 eval-requirements.txt 的 Python）才跑")


@needs_frameworks
def test_real_ragas_offline_metrics() -> None:
    rows = fw.run_bridge("ragas", FRAMEWORKS, [
        {"id": "hit", "user_input": "port?", "retrieved_contexts": ["The API listens on port 8080."], "reference_contexts": ["The API listens on port 8080."]},
        {"id": "miss", "user_input": "port?", "retrieved_contexts": ["Logs go to Loki."], "reference_contexts": ["The API listens on port 8080."]},
    ], args=("--offline",))
    assert rows[0]["scores"]["non_llm_context_recall"] == pytest.approx(1.0) and rows[1]["scores"]["non_llm_context_recall"] == pytest.approx(0.0)


class _Judge(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.headers["Authorization"] == f"Bearer {KEY}"
        answer = {"score": 9, "reason": "same facts"}
        data = json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}], "model": body["model"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@needs_frameworks
def test_real_deepeval_geval_through_the_judge_interface() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Judge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cfg = fw.JudgeConfig(f"http://127.0.0.1:{server.server_address[1]}/v1", KEY, "judge-x")
        [row] = fw.run_bridge("deepeval", FRAMEWORKS, [{"id": "c", "input": "total units?", "actual_output": "243", "expected_output": "243",
                                                      "metrics": [{"name": "correctness", **dv.METRICS["correctness"], "evaluation_steps": ["Compare the numbers."]}]}], judge=cfg)
    finally:
        server.shutdown()
    assert row["scores"]["correctness"]["score"] == pytest.approx(0.9) and row["scores"]["correctness"]["success"] is True
