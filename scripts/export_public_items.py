"""把能力矩阵会跑的公开数据集题目导出到 services/py-learning/evalsets/public（docs/plan/eval-site.md 第 3 节第 2 条）。

用各个 CellEval 自己的选题（`items(n, seed)`，与 `capability run` 同一套哈希排序）：默认 n = 40、seed = 0，即矩阵第一轮每格的前缀；
n 变大时只在后面追加，所以这份清单就是会跑的题。每题一行：id、question、answer，其余原字段放 extra（超长的截断，长篇上下文不收）。
数据从各适配器钉死的版本下载（`AR_EVAL_DATA_DIR`）。

  uv run --package agentruntime-learning python scripts/export_public_items.py [--n 40] [--seed 0] [--only simpleqa,frames]
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parents[1] / "services" / "py-learning" / "evalsets" / "public"
EVALS = ("bfcl", "tau2", "rewardbench2", "judgebench", "longbench", "simpleqa", "frames")
QUESTION_KEYS = ("question", "prompt", "problem", "instruction", "user_scenario", "messages")
ANSWER_KEYS = ("answer", "label", "possible_answer", "ground_truth", "evaluation_criteria", "chosen", "expected")
DROP_KEYS = {"context"}  # LongBench 的长篇原文：太长，许可各异，只给问题与选项
MAX_FIELD = 20_000
# 形似访问令牌的字符串（有的题故意放了假的，例如 Terminal-Bench 的 sanitize-git-repo）换成占位符：不进仓库，也不触发密钥扫描。
TOKEN_RE = re.compile(r"hf_[A-Za-z0-9]{30,}|ghp_[A-Za-z0-9]{30,}|gho_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}"
                      r"|sk-[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]{20,}")


def scrub(value: Any, hits: list[int]) -> Any:
    if isinstance(value, str):
        def rep(m: re.Match[str]) -> str:
            hits.append(1)
            return f"<redacted:{m.group(0)[:4]}…>"
        return TOKEN_RE.sub(rep, value)
    if isinstance(value, dict):
        return {k: scrub(v, hits) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, hits) for v in value]
    return value


def _trim(value: Any) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= MAX_FIELD:
        return value
    return text[:MAX_FIELD] + f"\n…（截断，原长 {len(text)} 字符）"


def _pick(payload: dict[str, Any], keys: tuple[str, ...]) -> tuple[str | None, Any]:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return key, payload[key]
    return None, None


def export(name: str, n: int, seed: int) -> Path:
    module = importlib.import_module(f"agentruntime_learning.capability.evals.{name}")
    cell = module.make()
    items = cell.items(n, seed)
    path = OUT / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for item in items:
            payload = dict(item.payload) if hasattr(item.payload, "items") else {"value": item.payload}
            if name == "longbench":  # 索引里只有元数据；问题与选项在逐题的缓存里（长篇原文不收）
                row = json.loads((cell.base() / "items" / f"{item.id}.json").read_text("utf-8"))
                payload.update({k: row[k] for k in ("question", "choice_A", "choice_B", "choice_C", "choice_D") if k in row})
            qk, question = _pick(payload, QUESTION_KEYS)
            ak, answer = _pick(payload, ANSWER_KEYS)
            extra = {k: _trim(v) for k, v in payload.items() if k not in {qk, ak, "id"} | DROP_KEYS}
            if "context" in payload:
                extra["context_omitted_chars"] = len(str(payload["context"]))
            row = {"id": item.id, "question": _trim(question if isinstance(question, str) else json.dumps(question, ensure_ascii=False, default=str)),
                   "answer": _trim(answer), "extra": extra, "selection": {"n": n, "seed": seed, "eval": name}}
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=40)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--only", default=",".join(EVALS))
    args = parser.parse_args(argv)
    failed = []
    for name in [x for x in args.only.split(",") if x]:
        try:
            print(name, "→", export(name, args.n, args.seed))
        except Exception as exc:  # noqa: BLE001 — 一个数据集取不到不挡别的
            failed.append(name)
            print(name, "失败：", type(exc).__name__, str(exc)[:200], file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
