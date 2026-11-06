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
import hashlib
import time
import json
import math
import os
import random
import sys
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
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
    # Official data may repeat a session ID with a different date. Retain every
    # occurrence while assigning unique round IDs in the original session group.
    next_round: dict[str, int] = defaultdict(int)
    for session in instance.sessions:
        turns = [t for t in session.turns if str(t.get("content") or "").strip()]
        n, i = next_round[session.id], 0
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
        next_round[session.id] = n
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


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(temporary, path)


def _read_rows(path: Path) -> dict[str, dict[str, Any]]:
    done: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text("utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                done[str(row["question_id"])] = row
    return done


async def _run(
    instances: Sequence[Instance], retriever: str | Callable[[list[dict[str, Any]]], Any], out_dir: Path, *, top_k: int = DEFAULT_TOP_K,
    ks: Sequence[int] = DEFAULT_KS, budget_usd: float | None = None, price_per_m: float = DEFAULT_EMBED_PRICE,
    provenance: Mapping[str, Any] | None = None, timeout_s: float = 600.0,
) -> dict[str, Any]:
    """Resume only an identical experiment. Journal each start before paid work;
    failed/interrupted attempts remain explicit and are never silently re-run.
    """
    if top_k <= 0 or top_k > DEFAULT_TOP_K or not ks or any(k <= 0 for k in ks) or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("invalid retrieval limits")
    if not math.isfinite(price_per_m) or price_per_m < 0 or budget_usd is not None and (not math.isfinite(budget_usd) or budget_usd < 0):
        raise ValueError("invalid budget")
    if len({i.question_id for i in instances}) != len(instances):
        raise ValueError("duplicate question_id")
    out_dir = Path(out_dir)
    results = out_dir / "results.jsonl"
    todo = [i for i in instances if not i.abstention]
    skipped = len(instances) - len(todo)
    name = retriever if isinstance(retriever, str) else f"{retriever.__module__}:{retriever.__qualname__}"
    manifest = {
        "schema_version": 2, "scoring_version": "longmemeval-retrieval-v1", "retriever": name,
        "dataset_digest": _digest([asdict(i) for i in instances]), "question_ids": [i.question_id for i in instances],
        "top_k": top_k, "ks": list(ks), "price_per_m": price_per_m, "budget_usd": budget_usd,
        "timeout_s": timeout_s, "max_round_chars": MAX_ROUND_CHARS, "provenance": dict(provenance or {}),
    }
    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists():
        if json.loads(manifest_path.read_text("utf-8")) != manifest:
            raise ValueError("experiment configuration or dataset changed; use a new output directory")
    elif results.exists() and results.stat().st_size:
        raise ValueError("legacy results lack immutable provenance; use a new output directory")
    done = _read_rows(results)
    if set(done) - {i.question_id for i in todo}:
        raise ValueError("results contain questions outside the frozen selection")
    pending = [i for i in todo if i.question_id not in done]
    if any(not r.get("cleanup_complete", False) for r in done.values()):
        pending = []  # Reconcile previous unknown writes before adding any new ones.
    # Check the whole frozen experiment, not a fresh allowance at each resume.
    estimate = 0.0 if retriever == "lexical" else estimate_usd(todo, price_per_m=price_per_m)
    if budget_usd is not None and estimate > budget_usd:
        raise BudgetExceeded(f"估计 embedding 要花 ${estimate:.4f}，超过预算 ${budget_usd:.4f}：一题都没跑")
    factory = (lambda corpus: rag.load_retriever(retriever, corpus)) if isinstance(retriever, str) else retriever
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(manifest_path, manifest)

    def append(row: dict[str, Any]) -> None:
        with results.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        done[row["question_id"]] = row

    for inst in pending:
        corpus = rounds(inst)
        row: dict[str, Any] = {"question_id": inst.question_id, "question_type": inst.question_type,
                               "rounds": len(corpus), "outcome": "interrupted", "session": metrics([], set(inst.answer_session_ids), ks=ks),
                               "turn": None, "ranked_sessions": [], "ranked_rounds": [], "cleanup_complete": False}
        append(row)
        backend = None
        started = time.monotonic()
        got: list[Any] = []
        try:
            backend = factory(corpus)
            lifecycle_sink = getattr(backend, "set_lifecycle_sink", None)
            if lifecycle_sink is not None:
                def checkpoint(diagnostics: dict[str, Any]) -> None:
                    row["lifecycle"] = dict(diagnostics)
                    append(row)
                lifecycle_sink(checkpoint)
            got = await asyncio.wait_for(backend.retrieve(inst.question, top_k), timeout=timeout_s)
            row["outcome"] = "completed"
        except Exception as exc:
            # Record class/code only; arbitrary provider errors may contain
            # credentials, URLs or private memory content.
            row["outcome"] = "timeout" if isinstance(exc, TimeoutError) else "environment_error"
            row["error_type"] = type(exc).__name__
        finally:
            if backend is not None:
                try:
                    closer = getattr(backend, "aclose", None)
                    if closer is not None:
                        closed = closer()
                        if inspect.isawaitable(closed):
                            await asyncio.wait_for(closed, timeout=timeout_s)
                    row["cleanup_complete"] = True
                except Exception as exc:
                    row["outcome"] = "cleanup_error"
                    row["cleanup_error_type"] = type(exc).__name__
                diagnostics = getattr(backend, "diagnostics", None)
                if isinstance(diagnostics, Mapping):
                    row["lifecycle"] = dict(diagnostics)
                    row["cleanup_complete"] = bool(diagnostics.get("cleanup_complete", row["cleanup_complete"]))
            else:
                row["cleanup_complete"] = True
            row["elapsed_s"] = round(time.monotonic() - started, 4)
        if row["outcome"] == "completed":
            round_ids = [str(g["id"]) for g in got]
            valid_ids = {r["id"] for r in corpus}
            if len(round_ids) != len(set(round_ids)) or any(rid not in valid_ids for rid in round_ids):
                row["outcome"] = "environment_error"
                row["error_type"] = "InvalidRetrievalIds"
            else:
                sessions = session_ranking(round_ids)
                evidence = {r["id"] for r in corpus if r["has_answer"]}
                row.update({"ranked_sessions": sessions[:max(ks)], "ranked_rounds": round_ids[:max(ks)],
                            "session": metrics(sessions, set(inst.answer_session_ids), ks=ks),
                            "turn": metrics(round_ids, evidence, ks=ks) if evidence else None})
        append(row)
        # A failed cleanup may leave queued writes; do not add more work until
        # its recorded workspace/IDs have been reconciled.
        if not row["cleanup_complete"]:
            break
    rows = [done[i.question_id] for i in todo if i.question_id in done]
    summary = _summary(rows, retriever=str(name), top_k=top_k, ks=ks, skipped=skipped)
    summary.update({"selected": len(todo), "completed": sum(r.get("outcome") == "completed" for r in rows),
                    "outcomes": {o: sum(r.get("outcome") == o for r in rows) for o in sorted({r.get("outcome", "unknown") for r in rows})},
                    "timeouts": sum(r.get("outcome") == "timeout" or bool(r.get("lifecycle", {}).get("timed_out")) for r in rows),
                    "cleanup_complete": all(r.get("cleanup_complete", False) for r in rows),
                    "estimated_embedding_usd": estimate, "actual_cost_usd": 0.0 if retriever == "lexical" else None,
                    "manifest_digest": _digest(manifest)})
    summary["valid"] = len(rows) == len(todo) and summary["completed"] == len(todo) and summary["cleanup_complete"] and not summary["timeouts"]
    _write_json(out_dir / "summary.json", summary)
    return summary


async def run(instances: Sequence[Instance], retriever: str | Callable[[list[dict[str, Any]]], Any], out_dir: Path, **kwargs: Any) -> dict[str, Any]:
    """One writer per experiment directory; OS releases the lock after a crash."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".run.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0, 2)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            acquire = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: (lock.seek(0), msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1))
        else:
            import fcntl
            acquire = lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as exc:
            raise ValueError("experiment output directory is already running") from exc
        try:
            return await _run(instances, retriever, directory, **kwargs)
        finally:
            release()


def compare(lexical_dir: Path, live_dir: Path) -> dict[str, Any]:
    """Frozen acceptance rule: 50 paired questions, >=10 percentage-point
    gain, one-sided exact McNemar p<=0.05, no timeout or cleanup failure.
    """
    dirs = (Path(lexical_dir), Path(live_dir))
    manifests = [json.loads((d / "manifest.json").read_text("utf-8")) for d in dirs]
    for key in ("dataset_digest", "question_ids", "top_k", "ks", "scoring_version", "max_round_chars"):
        if manifests[0].get(key) != manifests[1].get(key):
            raise ValueError(f"unpaired experiment: {key}")
    if manifests[0]["retriever"] != "lexical" or manifests[1]["retriever"] != "agentruntime_memory.eval_retriever:live_retriever":
        raise ValueError("acceptance requires lexical and deployed live_retriever")
    for key in ("data_sha256", "seed", "limit"):
        if manifests[0].get("provenance", {}).get(key) != manifests[1].get("provenance", {}).get(key):
            raise ValueError(f"unpaired experiment provenance: {key}")
    rows = [_read_rows(d / "results.jsonl") for d in dirs]
    if rows[0].keys() != rows[1].keys():
        raise ValueError("unpaired question results")
    pairs = [(rows[0][qid]["session"]["recall_any@10"], rows[1][qid]["session"]["recall_any@10"]) for qid in rows[0]]
    wins = sum(b > a for a, b in pairs)
    losses = sum(b < a for a, b in pairs)
    discordant = wins + losses
    p = sum(math.comb(discordant, k) for k in range(wins, discordant + 1)) / 2 ** discordant if discordant else 1.0
    gain = (wins - losses) / len(pairs) if pairs else 0.0
    summaries = [json.loads((d / "summary.json").read_text("utf-8")) for d in dirs]
    integrity = (all(s.get("valid") for s in summaries) and len(pairs) == 50
                 and all(r.get("outcome") == "completed" and r.get("cleanup_complete")
                         and not r.get("lifecycle", {}).get("timed_out") for rs in rows for r in rs.values()))
    return {"rule": "longmemeval-live-acceptance-20261106-v1", "questions": len(pairs), "wins": wins, "losses": losses,
            "recall_any_at_10_gain": gain, "exact_one_sided_p": p, "integrity_passed": integrity,
            "passed": integrity and gain >= 0.10 and p <= 0.05}


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
    lines += ["", f"完整性有效：{summary.get('valid', False)}；完成 {summary.get('completed', 0)}/{summary.get('selected', summary.get('questions'))}；"
              f"超时 {summary.get('timeouts', 0)}；清理完成 {summary.get('cleanup_complete', False)}。",
              f"费用：预计 embedding ${summary.get('estimated_embedding_usd', 0):.6f}；实际 {summary.get('actual_cost_usd', 'unknown')}（None 表示待账本核验）。"]
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
    cp = sub.add_parser("compare", help="固定门槛的 lexical/live 配对验收")
    cp.add_argument("--lexical", type=Path, required=True)
    cp.add_argument("--live", type=Path, required=True)
    rp.add_argument("--timeout-s", type=float, default=600.0)
    rp.add_argument("--runtime-revision")
    rp.add_argument("--registry-revision")
    rp.add_argument("--memory-version")
    sp = sub.add_parser("report", help="打印 summary.json 的表")
    sp.add_argument("--out", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    try:
        if args.command == "compare":
            comparison = compare(args.lexical, args.live)
            print(json.dumps(comparison, ensure_ascii=False, indent=2))
            return 0 if comparison["passed"] else 4
        if args.command == "fetch":
            print(f"取到 {fetch(args.url, args.out)} 题：{args.out}")
            return 0
        if args.command == "report":
            print(report(json.loads((args.out / "summary.json").read_text("utf-8"))))
            return 0
        instances = load(args.data)
        chosen = sample(instances, limit=args.limit, seed=args.seed) if args.limit else instances
        live = args.retriever == "agentruntime_memory.eval_retriever:live_retriever"
        if live and not all((args.runtime_revision, args.registry_revision, args.memory_version, args.budget_usd is not None)):
            raise ValueError("live requires --runtime-revision, --registry-revision, --memory-version and --budget-usd")
        topology = {}
        if live:
            from agentruntime_memory.eval_retriever import single_writer_evidence
            topology = single_writer_evidence(os.environ.get("AR_MEMORY_TOPOLOGY_EVIDENCE", ""))
            if args.memory_version not in topology["images"]:
                raise ValueError("--memory-version must match the deployed image in topology evidence")
        provenance = {"memory_topology": topology, "data_sha256": hashlib.sha256(args.data.read_bytes()).hexdigest(), "seed": args.seed, "limit": args.limit,
                      "runtime_revision": args.runtime_revision, "registry_revision": args.registry_revision,
                      "memory_version": args.memory_version, "pipeline_timeout_s": float(os.environ.get("AR_MEMORY_EVAL_WAIT_S", "300")) if live else None, "memory_address": os.environ.get("AR_MEMORY_ADDRESS", "") if live else "",
                      "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        summary = asyncio.run(run(chosen, args.retriever, args.out, top_k=args.top_k, ks=tuple(args.k or DEFAULT_KS),
                                  budget_usd=args.budget_usd, price_per_m=args.embed_price_per_m,
                                  provenance=provenance, timeout_s=args.timeout_s))
        print(report(summary))
        return 0 if summary["valid"] else 4
    except BudgetExceeded as exc:
        print(exc, file=sys.stderr)
        return 3
    except (OSError, ValueError, KeyError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


__all__ = ["BudgetExceeded", "Instance", "Session", "estimate_usd", "fetch", "load", "main", "metrics", "report", "rounds", "run", "sample", "session_ranking"]
