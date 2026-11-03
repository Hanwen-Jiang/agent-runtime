"""LongBench v2 的子集（docs/plan/capability-matrix-evals.md 第 3 节，role transform）：长上下文理解的四选一，按标准答案判。

数据：Hugging Face `zai-org/LongBench-v2`（原 THUDM/LongBench-v2）在钉死的提交上的 `data.json`（Apache-2.0；465 MB，503 道题），
核对 sha256 后逐条拆开缓存在 `<AR_EVAL_DATA_DIR>/longbench-v2/<提交>/`：`index.jsonl` 是每题的元数据，`items/<id>.json` 是整题，
data.json 拆完就删。默认只取 `length=short` 的题（上下文不到 3.2 万词）；`lengths`、`domains`、`difficulties` 可以改子集。

问法照官方（THUDM/LongBench 的 `pred.py` 与 `prompts/0shot.txt`，不带 CoT）：模板里填上下文、问题与四个选项，取回答里的
“The correct answer is (X)”（先去掉 `*`，再退一步认 “The correct answer is X”），和 answer 一样就对。
官方超长时按 token 截掉中间、只留头尾各一半（上限 12 万 token）；这里没有分词器，按 UTF-8 字节数估 token（约 4 字节一个，
中英文都大致如此），超过 `max_context_tokens`（默认 12 万）时同样截掉上下文的中间。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agentruntime_learning.capability.base import Item, ItemResult
from agentruntime_learning.capability.evals import _datasets as ds
from agentruntime_worker.executor.model_gateway import ModelGateway

NAME = "longbench-v2"
ROLE = "transform"
DATASET = "zai-org/LongBench-v2"
REVISION = "2b48e494f2c7a2f0af81aae178e05c7e1dde0fe9"  # 2024-12-20
DATA_SHA256 = "15d61c22d92c96900b3c4948b6aeea218d3214b676a65df48e7b8555604c7fe2"
LENGTHS = ("short", "medium", "long")
BYTES_PER_TOKEN = 4

TEMPLATE = """Please read the following text and answer the question below.

<text>
$DOC$
</text>

What is the correct answer to this question: $Q$
Choices:
(A) $C_A$
(B) $C_B$
(C) $C_C$
(D) $C_D$

Format your response as follows: "The correct answer is (insert answer here)"."""
META = ("_id", "domain", "sub_domain", "difficulty", "length", "answer")


def extract_answer(response: str) -> str | None:
    response = response.replace("*", "")
    m = re.search(r"The correct answer is \(([A-D])\)", response) or re.search(r"The correct answer is ([A-D])", response)
    return m.group(1) if m else None


def truncate_middle(text: str, max_bytes: int) -> tuple[str, bool]:
    """超过 max_bytes（UTF-8）时留头尾各一半、去掉中间（在字符边界上切）。"""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    half = max(max_bytes // 2, 0)
    head = raw[:half].decode("utf-8", "ignore")
    tail = raw[len(raw) - half:].decode("utf-8", "ignore")
    return head + tail, True


def build_prompt(row: dict[str, Any], max_context_tokens: int) -> tuple[str, bool]:
    def fill(context: str) -> str:
        return (TEMPLATE.replace("$DOC$", context.strip()).replace("$Q$", row["question"].strip()).replace("$C_A$", row["choice_A"].strip())
                .replace("$C_B$", row["choice_B"].strip()).replace("$C_C$", row["choice_C"].strip()).replace("$C_D$", row["choice_D"].strip()))

    overhead = len(fill("").encode("utf-8"))
    context, cut = truncate_middle(row["context"].strip(), max(max_context_tokens * BYTES_PER_TOKEN - overhead, 0))
    return fill(context), cut


def cache_dir() -> Path:
    return ds.data_dir() / NAME / REVISION


def build_cache(data_json: Path, base: Path) -> None:
    """把 data.json 拆成 index.jsonl 与 items/<id>.json（逐条读，不整个读进内存）。"""
    items = base / "items"
    items.mkdir(parents=True, exist_ok=True)
    index = []
    for row in ds.iter_json_array(data_json):
        rid = ds.safe_name(str(row["_id"]))
        ds.write_atomic(items / f"{rid}.json", json.dumps(row, ensure_ascii=False))
        index.append({**{k: row[k] for k in META}, "id": rid, "context_bytes": len(row["context"].encode("utf-8"))})
    ds.write_atomic(base / "index.jsonl", "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in index))


def load_index(base: Path | None = None, *, source: Path | None = None) -> list[dict[str, Any]]:
    """题目的元数据；缓存里没有时先取钉死的 data.json（或用 source 给的同格式文件）拆好。"""
    base = Path(base or cache_dir())
    if not (base / "index.jsonl").exists():
        if source is not None:
            build_cache(Path(source), base)
        else:
            data = ds.download(f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/data.json", base / "data.json", sha256=DATA_SHA256, timeout=1800)
            build_cache(data, base)
            data.unlink()
    return ds.read_jsonl(base / "index.jsonl")


class LongBenchEval:
    name = NAME
    role = ROLE
    est_tokens = (35000, 50)  # short 子集的上下文约 2.5 万～4.5 万 token

    def __init__(self, *, lengths: Sequence[str] = ("short",), domains: Sequence[str] | None = None, difficulties: Sequence[str] | None = None,
                 max_context_tokens: int = 120_000, max_output_tokens: int = 1024, cache: Path | None = None, source: Path | None = None) -> None:
        unknown = set(lengths) - set(LENGTHS)
        if unknown:
            raise ValueError(f"length 只有 {'、'.join(LENGTHS)}")
        self.lengths, self.domains, self.difficulties = tuple(lengths), tuple(domains or ()), tuple(difficulties or ())
        self.max_context_tokens, self.max_output_tokens = max_context_tokens, max_output_tokens
        self.cache, self.source = cache, source
        tag = "" if self.lengths == ("short",) else ":" + "+".join(self.lengths)
        tag += "".join(f":{'+'.join(x)}" for x in (self.domains, self.difficulties) if x)
        tag += "" if max_context_tokens == 120_000 else f":ctx{max_context_tokens}"
        self.version = f"hf@{REVISION[:7]}{tag}"
        self._index: dict[str, dict[str, Any]] | None = None

    def base(self) -> Path:
        return Path(self.cache or cache_dir())

    def index(self) -> dict[str, dict[str, Any]]:
        if self._index is None:
            rows = load_index(self.base(), source=self.source)
            self._index = {r["id"]: r for r in rows if r["length"] in self.lengths and (not self.domains or r["domain"] in self.domains)
                           and (not self.difficulties or r["difficulty"] in self.difficulties)}
        return self._index

    def items(self, n: int, seed: int) -> list[Item]:
        rows = self.index()
        return [Item(i, rows[i]) for i in ds.select(sorted(rows), n, seed)]

    async def run_item(self, item: Item, gateway: ModelGateway, *, candidate: str) -> ItemResult:
        meter = ds.Meter(gateway, item_id=item.id, eval_name=self.name)
        p = item.payload
        record: dict[str, Any] = {k: p.get(k) for k in ("domain", "sub_domain", "difficulty", "length", "context_bytes")}
        record.update({"answer": p.get("answer"), "candidate": candidate})
        try:
            row = json.loads((self.base() / "items" / f"{item.id}.json").read_text("utf-8"))
            prompt, cut = build_prompt(row, self.max_context_tokens)
            response = await meter.generate(role=ROLE, node="longbench", messages=[ds.chat("user", prompt)], max_output_tokens=self.max_output_tokens)
            pred = extract_answer(response.text)
            record.update({"truncated": cut, "response": response.text, "pred": pred})
            return meter.result(1.0 if pred == row["answer"] else 0.0, record)
        except Exception as exc:
            return meter.error(exc, record)


def make(**options: Any) -> LongBenchEval:
    return LongBenchEval(**options)


__all__ = ["LongBenchEval", "build_cache", "build_prompt", "extract_answer", "load_index", "make", "truncate_middle"]
