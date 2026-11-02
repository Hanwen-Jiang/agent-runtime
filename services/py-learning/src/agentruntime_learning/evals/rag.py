"""记忆检索的评测（docs/plan/feedback-2026-11-01.md N-13）：检索器按问题取回 top-k 条记忆，自己算 hit@k、recall@k、MRR，
再交给 Ragas 的桥（frameworks.run_bridge，单独的 Python）算上下文的精确率与召回率。

评测集（evalsets/memory-rag）：corpus.jsonl（id、kind、text）与 questions.jsonl（id、question、reference、reference_ids、confirmed）。

检索器（--retriever）：
- lexical：离线基线（TF-IDF 余弦，只用标准库），检查链路用；
- <模块>:<工厂函数>：工厂收 corpus（条目的列表），返回有 `async retrieve(question, top_k) -> [{"id", "text"}]` 的对象
  （可选 `async aclose()`）。Memory Service 的代码按这个接口接（例如在 py-memory 里用开发后端组装真的 MemoryService）。

Ragas：没有判分模型时只算不要模型的指标（--offline）；给了判分模型时另算 context_precision、context_recall。
边跑边发（suite ragas）：每个问题一条结果（取回了参考条目里的至少一条算 passed），结束发各指标的平均值。
"""

from __future__ import annotations

import importlib
import inspect
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from agentruntime_learning.evals import frameworks

SUITE = "ragas"
DEFAULT_SET = Path(__file__).resolve().parents[3] / "evalsets" / "memory-rag"
DEFAULT_TOP_K = 3


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    reference: str
    reference_ids: tuple[str, ...]
    confirmed: bool


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text("utf-8").splitlines() if line.strip()]


def load_set(root: Path = DEFAULT_SET) -> tuple[list[dict[str, str]], list[Question]]:
    corpus = [{"id": str(r["id"]), "kind": str(r.get("kind") or ""), "text": str(r["text"])} for r in _jsonl(Path(root) / "corpus.jsonl")]
    ids = [c["id"] for c in corpus]
    if len(set(ids)) != len(ids):
        raise ValueError("corpus.jsonl 里有重复的 id")
    questions = []
    for r in _jsonl(Path(root) / "questions.jsonl"):
        refs = tuple(str(x) for x in r.get("reference_ids") or ())
        missing = [x for x in refs if x not in ids]
        if not refs or missing:
            raise ValueError(f"{r.get('id')}：reference_ids 为空或不在 corpus 里：{missing}")
        questions.append(Question(str(r["id"]), str(r["question"]), str(r.get("reference") or ""), refs, bool(r.get("confirmed"))))
    if not questions:
        raise ValueError(f"{root} 里没有问题")
    return corpus, questions


class Retriever(Protocol):
    name: str

    async def retrieve(self, question: str, top_k: int) -> list[dict[str, str]]: ...


_TOKEN = re.compile(r"[a-z0-9_]+|[一-鿿]")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class LexicalRetriever:
    """离线基线：TF-IDF 余弦（英文按词、中文按字）。"""

    name = "lexical"

    def __init__(self, corpus: Sequence[Mapping[str, str]]) -> None:
        self.items = [dict(c) for c in corpus]
        docs = [Counter(_tokens(c["text"])) for c in self.items]
        df: Counter[str] = Counter(t for d in docs for t in d)
        n = len(docs)
        self.idf = {t: math.log((1 + n) / (1 + f)) + 1 for t, f in df.items()}
        self.vectors = [self._vec(d) for d in docs]

    def _vec(self, counts: Mapping[str, int]) -> dict[str, float]:
        v = {t: c * self.idf.get(t, 0.0) for t, c in counts.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    async def retrieve(self, question: str, top_k: int) -> list[dict[str, str]]:
        q = self._vec(Counter(_tokens(question)))
        ranked = sorted(range(len(self.items)), key=lambda i: (-sum(q.get(t, 0.0) * x for t, x in self.vectors[i].items()), self.items[i]["id"]))
        return [{"id": self.items[i]["id"], "text": self.items[i]["text"]} for i in ranked[:top_k]]


def load_retriever(spec: str, corpus: Sequence[Mapping[str, str]]) -> Any:
    if spec == "lexical":
        return LexicalRetriever(corpus)
    module, sep, attr = spec.partition(":")
    if not sep:
        raise ValueError(f"检索器是 lexical 或 <模块>:<工厂函数>，得到 {spec!r}")
    factory = getattr(importlib.import_module(module), attr)
    return factory(list(corpus))


def retrieval_metrics(retrieved: Sequence[str], reference: Sequence[str]) -> dict[str, float]:
    """hit（取回了至少一条参考的）、recall（参考里取回的比例）、mrr（第一条参考的排名的倒数）。"""
    ref = set(reference)
    first = next((i for i, x in enumerate(retrieved, 1) if x in ref), None)
    return {"hit": float(first is not None), "recall": len(ref & set(retrieved)) / len(ref) if ref else 0.0, "mrr": 1.0 / first if first else 0.0}


async def run(
    retriever: Any, corpus: Sequence[Mapping[str, str]], questions: Sequence[Question], out_dir: Path, *, top_k: int = DEFAULT_TOP_K,
    python: str | None = None, judge: frameworks.JudgeConfig | None = None, live: Any = None, bridge: Any = frameworks.run_bridge,
) -> dict[str, Any]:
    """检索、打分，写 <out_dir>/results.jsonl 与 summary.json，返回 summary。python 为空时不跑 Ragas，只给自己算的指标。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    texts = {c["id"]: c["text"] for c in corpus}
    if live is not None:
        await live.planned(len(questions))
    rows = []
    for q in questions:
        got = await retriever.retrieve(q.question, top_k)
        ids = [str(g["id"]) for g in got]
        own = retrieval_metrics(ids, q.reference_ids)
        rows.append({"id": q.id, "retrieved_ids": ids, "reference_ids": list(q.reference_ids), **own,
                     "_bridge": {"id": q.id, "user_input": q.question, "retrieved_contexts": [str(g["text"]) for g in got],
                                 "reference_contexts": [texts[i] for i in q.reference_ids], "reference": q.reference or None}})
        if live is not None:
            await live.case("passed" if own["hit"] else "failed")
    ragas: dict[str, Any] = {}
    if python:
        results = bridge("ragas", python, [r["_bridge"] for r in rows], judge=judge, args=() if judge else ("--offline",))
        for row, res in zip(rows, results, strict=True):
            row["ragas"] = res.get("scores") or {}
            if res.get("errors"):
                row["ragas_errors"] = res["errors"]
    for row in rows:
        row.pop("_bridge")
    means: dict[str, float | None] = {}
    for name in ("hit", "recall", "mrr"):
        means[name] = round(sum(r[name] for r in rows) / len(rows), 4)
    for name in sorted({k for r in rows for k in r.get("ragas", {})}):
        values = [r["ragas"][name] for r in rows if r.get("ragas", {}).get(name) is not None]
        means[name] = round(sum(values) / len(values), 4) if values else None
    summary = {
        "framework": "ragas", "retriever": getattr(retriever, "name", type(retriever).__name__), "top_k": top_k, "questions": len(rows),
        "metrics": means, "ragas": "judged" if (python and judge) else "offline" if python else "skipped",
        "judge_model": judge.model if judge else None, "unconfirmed": [q.id for q in questions if not q.confirmed],
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (out_dir / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", "utf-8")
    if live is not None:
        for name, value in means.items():
            if value is not None:
                await live.score(name, value)
        await live.finished(model=summary["retriever"])
    closer = getattr(retriever, "aclose", None)
    if closer is not None:
        result = closer()
        if inspect.isawaitable(result):
            await result
    return summary


__all__ = ["DEFAULT_SET", "SUITE", "LexicalRetriever", "Question", "load_retriever", "load_set", "retrieval_metrics", "run"]
