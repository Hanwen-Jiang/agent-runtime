"""LongMemEval_S：测 Memory Service 的检索（docs/plan/memory-live.md 第 5 节，F2）。

    python -m agentruntime_learning.benchmarks longmemeval fetch --out <longmemeval_s.json>
    python -m agentruntime_learning.benchmarks longmemeval run --data <文件> --out <目录> --retriever <检索器> --limit 50 --seed 0 [--budget-usd X]
    python -m agentruntime_learning.benchmarks longmemeval report --out <目录>

- 题库：清洗过的 LongMemEval（ICLR 2025，MIT）的 S 版，每题约 50 个会话的对话历史；数据不进仓库。
- 每题一个检索器，只写这一题的历史。按轮切：一轮是一条用户发言加上紧跟着的助手回复，文字前面带会话日期；
  原数据的 has_answer 标出证据所在的轮。
- 检索器同 evals ragas 的 --retriever：lexical（离线基线），或 <模块>:<工厂函数>（工厂收这一题的轮，返回有
  `async retrieve(question, top_k)` 的对象，可选 `async aclose()`）；例如 agentruntime_memory.eval_retriever:dev_retriever
  （进程内的 MemoryService）与 :live_retriever（已部署的 Memory Service）。
- 指标按 LongMemEval 官方的检索评测：会话级的 recall_any@k、recall_all@k、ndcg_any@k（会话排名由轮的排名去重得到），
  轮级同样三项（证据轮为 has_answer 的轮）。拒答题（question_id 以 _abs 结尾）没有证据，不跑，只记个数。
- 按题型分层抽题；同一个 --out 再跑是续跑；开跑前按字数估 embedding 的花费，超过 --budget-usd 就不跑。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import math
import os
import random
import sys
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentruntime_learning.evals import rag

DEFAULT_URL = "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json"
DEFAULT_TOP_K = 50  # Memory Service 的 Search 最多给 50 条
DEFAULT_KS = (5, 10, 30)
DEFAULT_EMBED_PRICE = 0.13  # text-embedding-3-large，美元每百万 token
CHARS_PER_TOKEN = 4
MAX_ROUND_CHARS = 24000  # embedding 的输入上限约 8k token；更长的轮截断
_REQUIRED = ("question_id", "question_type", "question", "answer", "haystack_session_ids", "haystack_dates", "haystack_sessions", "answer_session_ids")


class BudgetExceeded(Exception):
    """估出来的花费超过预算：一题都不跑。"""


@dataclass(frozen=True)
class Session:
    id: str
    date: str
    turns: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class Instance:
    question_id: str
    question_type: str
    question: str
    answer: str
    question_date: str
    sessions: tuple[Session, ...]
    answer_session_ids: tuple[str, ...]

    @property
    def abstention(self) -> bool:
        return self.question_id.endswith("_abs")


def _parse(raw: Any) -> Instance:
    if not isinstance(raw, dict) or any(k not in raw for k in _REQUIRED):
        missing = [k for k in _REQUIRED if not isinstance(raw, dict) or k not in raw]
        raise ValueError(f"不是 LongMemEval 的题：缺 {'、'.join(missing)}")
    ids, dates, sessions = raw["haystack_session_ids"], raw["haystack_dates"], raw["haystack_sessions"]
    if not (len(ids) == len(dates) == len(sessions)):
        raise ValueError(f"{raw['question_id']}：haystack 的会话、日期与编号个数不一致")
    return Instance(
        question_id=str(raw["question_id"]), question_type=str(raw["question_type"]), question=str(raw["question"]), answer=str(raw["answer"]),
        question_date=str(raw.get("question_date") or ""),
        sessions=tuple(Session(str(i), str(d), tuple(t for t in s if isinstance(t, dict))) for i, d, s in zip(ids, dates, sessions, strict=True)),
        answer_session_ids=tuple(str(x) for x in raw["answer_session_ids"]),
    )


def load(path: Path) -> list[Instance]:
    data = json.loads(Path(path).read_text("utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"{path}：要一个非空的 JSON 数组")
    return [_parse(item) for item in data]


def rounds(instance: Instance) -> list[dict[str, Any]]:
    """这一题的历史按轮切成记忆：{"id": "<会话>:<序号>", "kind", "text", "session", "has_answer"}。"""
    out: list[dict[str, Any]] = []
    for session in instance.sessions:
        turns = [t for t in session.turns if str(t.get("content") or "").strip()]
        n, i = 0, 0
        while i < len(turns):
            group = [turns[i]]
            if str(turns[i].get("role")) == "user" and i + 1 < len(turns) and str(turns[i + 1].get("role")) == "assistant":
                group.append(turns[i + 1])
            i += len(group)
            body = "\n".join(f"{t.get('role')}: {str(t.get('content')).strip()}" for t in group)
            text = f"[{session.date}] {body}"[:MAX_ROUND_CHARS]
            out.append({"id": f"{session.id}:{n}", "kind": "fact", "text": text, "session": session.id,
                        "has_answer": any(bool(t.get("has_answer")) for t in group)})
            n += 1
    return out


def sample(instances: Sequence[Instance], *, limit: int, seed: int) -> list[Instance]:
    """按题型分层、轮流取，同一个种子同样的结果；拒答题不取。"""
    groups: dict[str, list[Instance]] = defaultdict(list)
    for inst in sorted(instances, key=lambda i: i.question_id):
        if not inst.abstention:
            groups[inst.question_type].append(inst)
    rng = random.Random(seed)
    for qtype in sorted(groups):
        rng.shuffle(groups[qtype])
    picked: list[Instance] = []
    while len(picked) < limit and any(groups.values()):
        for qtype in sorted(groups):
            if groups[qtype] and len(picked) < limit:
                picked.append(groups[qtype].pop(0))
    return picked


def session_ranking(round_ids: Sequence[str]) -> list[str]:
    out: list[str] = []
    for rid in round_ids:
        session = rid.rsplit(":", 1)[0]
        if session not in out:
            out.append(session)
    return out


def metrics(ranked: Sequence[str], correct: set[str] | frozenset[str], *, ks: Sequence[int] = DEFAULT_KS) -> dict[str, float]:
    """LongMemEval 的检索指标：recall_any（前 k 里至少有一个证据）、recall_all（证据都在前 k 里）、ndcg_any（二值相关的 NDCG）。"""
    out: dict[str, float] = {}
    for k in ks:
        top = list(ranked[:k])
        dcg = sum(1 / math.log2(i + 2) for i, x in enumerate(top) if x in correct)
        idcg = sum(1 / math.log2(i + 2) for i in range(min(len(correct), k)))
        out[f"recall_any@{k}"] = float(any(x in correct for x in top))
        out[f"recall_all@{k}"] = float(bool(correct) and all(c in top for c in correct))
        out[f"ndcg_any@{k}"] = dcg / idcg if idcg else 0.0
    return out


def estimate_usd(instances: Sequence[Instance], *, price_per_m: float = DEFAULT_EMBED_PRICE) -> float:
    """写入与查询的 embedding 花费（按 4 个字符一个 token 估）。"""
    chars = sum(len(r["text"]) for inst in instances for r in rounds(inst)) + sum(len(inst.question) for inst in instances)
    return chars / CHARS_PER_TOKEN / 1_000_000 * price_per_m


def _mean(rows: Sequence[Mapping[str, float]]) -> dict[str, float]:
    keys = list(rows[0]) if rows else []
    return {k: round(sum(r[k] for r in rows) / len(rows), 4) for k in keys}


def _summary(rows: Sequence[Mapping[str, Any]], *, retriever: str, top_k: int, ks: Sequence[int], skipped: int) -> dict[str, Any]:
    by_type: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_type[str(row["question_type"])].append(row)
    turn_rows = [r["turn"] for r in rows if r.get("turn")]
    return {
        "benchmark": "longmemeval_s", "retriever": retriever, "top_k": top_k, "ks": list(ks), "questions": len(rows), "skipped_abstention": skipped,
        "session": _mean([r["session"] for r in rows]), "turn": _mean(turn_rows),
        "by_type": {t: {"questions": len(rs), "session": _mean([r["session"] for r in rs])} for t, rs in sorted(by_type.items())},
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


async def run(
    instances: Sequence[Instance], retriever: str | Callable[[list[dict[str, Any]]], Any], out_dir: Path, *, top_k: int = DEFAULT_TOP_K,
    ks: Sequence[int] = DEFAULT_KS, budget_usd: float | None = None, price_per_m: float = DEFAULT_EMBED_PRICE,
) -> dict[str, Any]:
    """逐题检索、打分，结果一题一行追加到 <out_dir>/results.jsonl（续跑读它），写 summary.json 并返回。"""
    out_dir = Path(out_dir)
    results = out_dir / "results.jsonl"
    todo = [i for i in instances if not i.abstention]
    skipped = len(instances) - len(todo)
    done: dict[str, dict[str, Any]] = {}
    if results.exists():
        for line in results.read_text("utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                done[str(row["question_id"])] = row
    pending = [i for i in todo if i.question_id not in done]
    if budget_usd is not None:
        estimate = estimate_usd(pending, price_per_m=price_per_m)
        if estimate > budget_usd:
            raise BudgetExceeded(f"估计 embedding 要花 ${estimate:.4f}，超过预算 ${budget_usd:.4f}：一题都没跑")
    factory = (lambda corpus: rag.load_retriever(retriever, corpus)) if isinstance(retriever, str) else retriever
    name = retriever if isinstance(retriever, str) else ""
    out_dir.mkdir(parents=True, exist_ok=True)
    for inst in pending:
        corpus = rounds(inst)
        backend = factory(corpus)
        name = str(getattr(backend, "name", "") or name)
        try:
            got = await backend.retrieve(inst.question, top_k)
        finally:
            closer = getattr(backend, "aclose", None)
            if closer is not None:
                result = closer()
                if inspect.isawaitable(result):
                    await result
        round_ids = [str(g["id"]) for g in got]
        sessions = session_ranking(round_ids)
        evidence = {r["id"] for r in corpus if r["has_answer"]}
        row = {
            "question_id": inst.question_id, "question_type": inst.question_type, "rounds": len(corpus),
            "ranked_sessions": sessions[:max(ks)], "ranked_rounds": round_ids[:max(ks)],
            "session": metrics(sessions, set(inst.answer_session_ids), ks=ks),
            "turn": metrics(round_ids, evidence, ks=ks) if evidence else None,
        }
        with results.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        done[inst.question_id] = row
    if not name and (out_dir / "summary.json").exists():
        name = str(json.loads((out_dir / "summary.json").read_text("utf-8")).get("retriever") or "")
    rows = [done[i.question_id] for i in todo if i.question_id in done]
    summary = _summary(rows, retriever=name, top_k=top_k, ks=ks, skipped=skipped)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return summary


def fetch(url: str, out: Path) -> int:
    """下载到临时文件，校验是 LongMemEval 的格式再改名；返回题数。"""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "agentruntime-longmemeval"}), timeout=600) as resp, tmp.open("wb") as fh:  # noqa: S310 — 固定的数据集地址或人给的地址
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
        count = len(load(tmp))
        os.replace(tmp, out)
        return count
    finally:
        tmp.unlink(missing_ok=True)


def report(summary: Mapping[str, Any]) -> str:
    ks = summary.get("ks") or []
    names = [f"{m}@{k}" for k in ks for m in ("recall_any", "recall_all", "ndcg_any")]
    lines = [f"# LongMemEval_S：{summary.get('retriever')}（{summary.get('questions')} 题，拒答题 {summary.get('skipped_abstention')} 道没跑，top {summary.get('top_k')}）", "",
             "| 范围 | 题数 | " + " | ".join(names) + " |", "| --- | --- | " + " | ".join("---" for _ in names) + " |"]

    def row(label: str, n: Any, values: Mapping[str, float]) -> str:
        return f"| {label} | {n} | " + " | ".join(f"{values.get(m, 0):.3f}" for m in names) + " |"

    lines.append(row("会话级", summary.get("questions"), summary.get("session") or {}))
    if summary.get("turn"):
        lines.append(row("轮级", "", summary["turn"]))
    for qtype, data in (summary.get("by_type") or {}).items():
        lines.append(row(f"会话级 · {qtype}", data.get("questions"), data.get("session") or {}))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agentruntime_learning.benchmarks longmemeval", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    fp = sub.add_parser("fetch", help="下载 LongMemEval_S（清洗版）")
    fp.add_argument("--url", default=DEFAULT_URL)
    fp.add_argument("--out", type=Path, required=True)
    rp = sub.add_parser("run", help="逐题写入、检索、打分")
    rp.add_argument("--data", type=Path, required=True)
    rp.add_argument("--out", type=Path, required=True, help="输出目录；同一个目录再跑是续跑")
    rp.add_argument("--retriever", default="lexical", help="lexical 或 <模块>:<工厂函数>")
    rp.add_argument("--limit", type=int, help="按题型分层抽这么多题；不给时跑全部非拒答题")
    rp.add_argument("--seed", type=int, default=0)
    rp.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    rp.add_argument("--k", type=int, action="append", help=f"报告的 k，可以给几个（默认 {', '.join(map(str, DEFAULT_KS))}）")
    rp.add_argument("--budget-usd", type=float)
    rp.add_argument("--embed-price-per-m", type=float, default=DEFAULT_EMBED_PRICE)
    sp = sub.add_parser("report", help="打印 summary.json 的表")
    sp.add_argument("--out", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    try:
        if args.command == "fetch":
            print(f"取到 {fetch(args.url, args.out)} 题：{args.out}")
            return 0
        if args.command == "report":
            print(report(json.loads((args.out / "summary.json").read_text("utf-8"))))
            return 0
        instances = load(args.data)
        chosen = sample(instances, limit=args.limit, seed=args.seed) if args.limit else instances
        summary = asyncio.run(run(chosen, args.retriever, args.out, top_k=args.top_k, ks=tuple(args.k or DEFAULT_KS),
                                  budget_usd=args.budget_usd, price_per_m=args.embed_price_per_m))
        print(report(summary))
        return 0
    except BudgetExceeded as exc:
        print(exc, file=sys.stderr)
        return 3
    except (OSError, ValueError, KeyError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


__all__ = ["BudgetExceeded", "Instance", "Session", "estimate_usd", "fetch", "load", "main", "metrics", "report", "rounds", "run", "sample", "session_ranking"]
