"""JudgeBench（docs/plan/capability-matrix-evals.md 第 3 节，role judge）：被测模型当裁判比较两个回答，和标准答案（label）比，不要别的裁判。

数据：Hugging Face `ScalerLab/JudgeBench` 在钉死的提交上的 JSONL（MIT），核对 sha256 后缓存在
`<AR_EVAL_DATA_DIR>/judgebench/<提交>/`。默认用 `gpt` 一份（GPT-4o 的回答，350 对，官方排行榜用的这份）；`claude` 一份（270 对，
Claude 3.5 Sonnet 的回答）可选。题目来自 MMLU-Pro、LiveBench、LiveCodeBench，每对回答有一个客观上对的（label 是 A>B 或 B>A）。

问法照官方的 Arena-Hard 裁判（JudgeBench `utils/judges.py` 的 ArenaHard 与 `utils/templates/arena_hard_judge_*.jinja2`）：系统提示 +
两个回答，结论是 `[[A>>B]]`、`[[A>B]]`、`[[A=B]]`、`[[B>A]]`、`[[B>>A]]` 之一（>> 记作 >）；没给出结论时追问一次
“continue your judgment and finish by outputting a final verdict label”（官方最多两轮）。`allow_tie=False` 换官方的 notie 模板。

位置偏差：两种顺序各问一次（第二次 A、B 对调，结论再翻回来）。默认 `scoring="consistent"`：两次都判对才算对（人定：只算前后一致的
正确答案）；官方的算法（两次里判对的次数减判反的次数大于 0 就算对，所以“一次对、一次平局”也算对）另记在 record 的
`official_correct`，`scoring="official"` 时按它给分。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agentruntime_learning.capability.base import Item, ItemResult
from agentruntime_learning.capability.evals import _datasets as ds
from agentruntime_worker.executor.model_gateway import ModelGateway

NAME = "judgebench"
ROLE = "judge"
DATASET = "ScalerLab/JudgeBench"
REVISION = "57dd5e0b9817d07f05ec8f45a91b2ce1e310e308"  # 2024-10-09
SPLITS = {
    "gpt": ("data/gpt-00000-of-00001.jsonl", "d5106306b38dead1b2e964ecf5534a3e7b2fc4b8d3b8da327599edfcebd76042"),
    "claude": ("data/claude-00000-of-00001.jsonl", "3b70504588e400613bf5744a20a345da8701b7150be9a30c9db0e6f3d7b70a62"),
}

_SYSTEM_HEAD = """Please act as an impartial judge and evaluate the quality of the responses provided by two AI assistants to the user prompt displayed below. You will be given assistant A's answer and assistant B's answer. Your job is to evaluate which assistant's answer is better.

Begin your evaluation by generating your own answer to the prompt. You must provide your answers before judging any answers.

When evaluating the assistants' answers, compare both assistants' answers with your answer. You must identify and correct any mistakes or inaccurate information.

Then consider if the assistant's answers are helpful, relevant, and concise. Helpful means the answer correctly responds to the prompt or follows the instructions. Note when user prompt has any ambiguity or more than one interpretation, it is more helpful and appropriate to ask for clarifications or more information from the user than providing an answer based on assumptions. Relevant means all parts of the response closely connect or are appropriate to what is being asked. Concise means the response is clear and not verbose or excessive.

Then consider the creativity and novelty of the assistant's answers when needed. Finally, identify any missing important information in the assistants' answers that would be beneficial to include when responding to the user prompt.

After providing your explanation, you must output only one of the following choices as your final verdict with a label:

"""
SYSTEM = _SYSTEM_HEAD + """1. Assistant A is significantly better: [[A>>B]]
2. Assistant A is slightly better: [[A>B]]
3. Tie, relatively the same: [[A=B]]
4. Assistant B is slightly better: [[B>A]]
5. Assistant B is significantly better: [[B>>A]]

Example output: "My final verdict is tie: [[A=B]]"."""
# 官方模板第一行末尾有一个空格，照抄。
SYSTEM_NOTIE = _SYSTEM_HEAD + "1. Assistant A is better: [[A>B]] \n" + """2. Assistant B is better: [[B>A]]

Example output: "My final verdict is Assistant A is better: [[A>B]]"."""
USER = """<|User Prompt|>
{prompt}

<|The Start of Assistant A's Answer|>
{answer_a}
<|The End of Assistant A's Answer|>

<|The Start of Assistant B's Answer|>
{answer_b}
<|The End of Assistant B's Answer|>"""
CONTINUE = "continue your judgment and finish by outputting a final verdict label"
VERDICT = re.compile(r"\[\[([AB<>=]+)\]\]")
ATTEMPTS = 2


def parse_verdict(text: str) -> tuple[str | None, bool]:
    """(结论, 要不要追问)：同官方的 get_score——没有标签时追问；标签不止一种时没有结论、也不再追问。"""
    found = {m for m in VERDICT.findall(text) if m}
    if not found:
        return None, True
    if len(found) == 1:
        return found.pop().replace(">>", ">").strip(), False
    return None, False


def flip(decision: str | None) -> str | None:
    return {"A>B": "B>A", "B>A": "A>B"}.get(decision or "", decision)


def official_correct(label: str, first: str | None, second_flipped: str | None) -> bool:
    """JudgeBench `utils/metrics.py` 的 compute_final_metrics（reverse_order）：判对的次数减判反的次数大于 0。"""
    counter = sum((d == label) - (d == flip(label)) for d in (first, second_flipped))
    return counter > 0


def load(splits: Sequence[str], root: Path | None = None) -> list[dict[str, Any]]:
    rows = []
    for split in splits:
        if split not in SPLITS:
            raise ValueError(f"JudgeBench 没有 {split!r} 这一份：只有 {'、'.join(SPLITS)}")
        path, sha = SPLITS[split]
        if root is None:
            file = ds.download(f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/{path}",
                               ds.data_dir() / "judgebench" / REVISION / Path(path).name, sha256=sha)
        else:
            file = Path(root) / Path(path).name
        for r in ds.read_jsonl(file):
            if r["label"] not in ("A>B", "B>A"):
                raise ValueError(f"{r['pair_id']} 的 label 是 {r['label']!r}")
            rows.append({"id": f"{split}:{r['pair_id']}", "split": split, "source": r["source"], "question": r["question"],
                         "response_A": r["response_A"], "response_B": r["response_B"], "label": r["label"]})
    return rows


class JudgeBenchEval:
    name = NAME
    role = ROLE
    est_tokens = (5000, 2400)  # 两种顺序各一次，Arena-Hard 要裁判先自己作答

    def __init__(self, *, splits: Sequence[str] = ("gpt",), allow_tie: bool = True, scoring: str = "consistent", root: Path | None = None,
                 max_output_tokens: int = 4096) -> None:
        if scoring not in ("consistent", "official"):
            raise ValueError("scoring 只能是 consistent 或 official")
        self.splits, self.allow_tie, self.scoring, self.root = tuple(splits), allow_tie, scoring, root
        self.max_output_tokens = max_output_tokens
        tag = "" if self.splits == ("gpt",) else ":" + "+".join(self.splits)
        tag += "" if allow_tie else ":notie"
        tag += "" if scoring == "consistent" else ":official"
        self.version = f"hf@{REVISION[:7]}{tag}"
        self._rows: dict[str, dict[str, Any]] | None = None

    def rows(self) -> dict[str, dict[str, Any]]:
        if self._rows is None:
            self._rows = {r["id"]: r for r in load(self.splits, self.root)}
        return self._rows

    def items(self, n: int, seed: int) -> list[Item]:
        rows = self.rows()
        return [Item(i, rows[i]) for i in ds.select(sorted(rows), n, seed)]

    async def _judge(self, meter: ds.Meter, node: str, question: str, a: str, b: str) -> dict[str, Any]:
        messages = [ds.chat("system", SYSTEM if self.allow_tie else SYSTEM_NOTIE), ds.chat("user", USER.format(prompt=question, answer_a=a, answer_b=b))]
        judgment = ""
        decision: str | None = None
        for _ in range(ATTEMPTS):
            response = await meter.generate(role=ROLE, node=node, messages=messages, max_output_tokens=self.max_output_tokens)
            judgment += "\n" + response.text
            decision, again = parse_verdict(judgment)
            messages = [*messages, ds.chat("assistant", response.text)]
            if not again:
                break
            messages.append(ds.chat("user", CONTINUE))
        return {"decision": decision, "judgment": judgment}

    async def run_item(self, item: Item, gateway: ModelGateway, *, candidate: str) -> ItemResult:
        meter = ds.Meter(gateway, item_id=item.id, eval_name=self.name)
        p = item.payload
        record: dict[str, Any] = {"source": p["source"], "label": p["label"], "candidate": candidate}
        try:
            first = await self._judge(meter, "judgebench.ab", p["question"], p["response_A"], p["response_B"])
            second = await self._judge(meter, "judgebench.ba", p["question"], p["response_B"], p["response_A"])
            d1, d2 = first["decision"], flip(second["decision"])
            consistent = d1 == p["label"] and d2 == p["label"]
            official = official_correct(p["label"], d1, d2)
            record.update({"trials": [first, {**second, "decision_flipped": d2}], "consistent_correct": consistent, "official_correct": official})
            return meter.result(1.0 if (consistent if self.scoring == "consistent" else official) else 0.0, record)
        except Exception as exc:
            return meter.error(exc, record)


def make(**options: Any) -> JudgeBenchEval:
    return JudgeBenchEval(**options)


__all__ = ["JudgeBenchEval", "flip", "load", "make", "official_correct", "parse_verdict"]
