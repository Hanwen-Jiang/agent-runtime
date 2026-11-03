"""FRAMES（docs/plan/capability-matrix-evals.md 第 3 节，role deliver）：要综合 2～15 篇维基百科条目的多跳问题；裁判按论文的自动评分提示判。

数据：Hugging Face `google/frames-benchmark` 在钉死的提交上的 `test.tsv`（824 题；Apache-2.0），核对 sha256 后缓存在
`<AR_EVAL_DATA_DIR>/frames/<提交>/`。题号是 TSV 第一列的序号（`frames-<序号>`）；reasoning_types 记进 record。

被测一侧用论文的 naive prompting：题目原样作为唯一的用户消息，不给检索到的条目（论文里 Gemini-Pro-1.5 这样是 40.8%）。
给条目原文的 oracle 设定要另取维基百科的页面（版本会变，要先定怎么钉），没做，见实现记录。

裁判用论文附录的 Auto-rating Prompt（Krishna et al. 2024, arXiv:2409.12941 图 6）：系统消息 "You are a helpful assistant."，
看题目、回答与标准答案，回 "Explanation:" 与 "Decision:"（TRUE 或 FALSE）；TRUE 记 1。找不到 Decision 时这道题记为出错。
裁判的调用、记录与同家冲突见 `_judged`。
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

from agentruntime_learning.capability.base import Item, ItemResult
from agentruntime_learning.capability.evals import _datasets as ds
from agentruntime_learning.capability.evals import _judged
from agentruntime_worker.executor.model_gateway import ModelGateway

NAME = "frames"
ROLE = "deliver"
DATASET = "google/frames-benchmark"
REVISION = "58d9fb6330f3ab1316d1eca12e5e8ef23dcc22ef"  # 2024-10-15
TSV_SHA256 = "4255093c93b595b5b04c7c8dde290b48ec87d72ca0fb0b760d9dd02740d669ff"

JUDGE_SYSTEM = "You are a helpful assistant."
JUDGE_TEMPLATE = """===Task===

I need your help in evaluating an answer provided by an LLM against a ground truth answer. Your task is to determine if the ground truth answer is present in the LLM's response. Please analyze the provided data and make a decision.

===Instructions===
1. Carefully compare the "Predicted Answer" with the "Ground Truth Answer".
2. Consider the substance of the answers – look for equivalent information or correct answers. Do not focus on exact wording unless the exact wording is crucial to the meaning.
3. Your final decision should be based on whether the meaning and the vital facts of the "Ground Truth Answer" are present in the "Predicted Answer:"

===Input Data===
- Question: {question}
- Predicted Answer: {response}
- Ground Truth Answer: {gold}

===Output Format===
Provide your final evaluation in the following format:
"Explanation:" (How you made the decision?)
"Decision:" ("TRUE" or "FALSE" )

Please proceed with the evaluation."""
DECISION = re.compile(r"Decision\W*(TRUE|FALSE)\b", re.IGNORECASE)
EXPLANATION = re.compile(r"Explanation\W*(.*?)(?:\n\W*Decision|\Z)", re.IGNORECASE | re.DOTALL)


def parse_decision(text: str) -> tuple[bool | None, str]:
    """(TRUE/FALSE，没有时 None；理由)。取最后一个 Decision（裁判先复述格式时不会被前面的示例骗到）。"""
    found = DECISION.findall(text.replace("*", ""))
    m = EXPLANATION.search(text.replace("*", ""))
    reason = (m.group(1).strip() if m else text.strip())
    return (found[-1].upper() == "TRUE") if found else None, reason


def load(source: Path | None = None) -> list[dict[str, Any]]:
    path = Path(source) if source else ds.download(f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/test.tsv",
                                                   ds.data_dir() / NAME / REVISION / "test.tsv", sha256=TSV_SHA256)
    csv.field_size_limit(1 << 24)
    rows = csv.DictReader(io.StringIO(path.read_text("utf-8")), delimiter="\t")
    return [{"id": f"frames-{r['']}", "prompt": r["Prompt"], "answer": r["Answer"], "reasoning_types": r.get("reasoning_types", ""),
             "wiki_links": r.get("wiki_links", "")} for r in rows]


class FramesEval:
    name = NAME
    role = ROLE
    version = f"hf@{REVISION[:7]}:naive"
    uses_judge = True  # 裁判调用经 RecordingJudgeGateway（见 _judged）
    est_tokens = (100, 800)
    est_other_usd = 0.005  # Opus 裁判约 0.4k 输入、0.15k 输出

    def __init__(self, *, source: Path | None = None, judge_log: Path | str | None = None, max_output_tokens: int = 4096) -> None:
        self.source = source
        self.log_path = _judged.judge_log_path(NAME, judge_log)
        self.max_output_tokens = max_output_tokens
        self._rows: dict[str, dict[str, Any]] | None = None

    def rows(self) -> dict[str, dict[str, Any]]:
        if self._rows is None:
            self._rows = {r["id"]: r for r in load(self.source)}
        return self._rows

    def items(self, n: int, seed: int) -> list[Item]:
        rows = self.rows()
        return [Item(i, rows[i]) for i in ds.select(sorted(rows), n, seed)]

    async def run_item(self, item: Item, gateway: ModelGateway, *, candidate: str) -> ItemResult:
        meter = ds.Meter(gateway, item_id=item.id, eval_name=self.name)
        p = item.payload
        record: dict[str, Any] = {"question": p["prompt"], "gold": p["answer"], "reasoning_types": p.get("reasoning_types", ""),
                                  "candidate": candidate, "same_vendor": _judged.same_vendor(candidate)}
        try:
            response = await meter.generate(role=ROLE, node="frames.answer", messages=[ds.chat("user", p["prompt"])],
                                            max_output_tokens=self.max_output_tokens)
            record["answer"] = response.text
            messages = [ds.chat("system", JUDGE_SYSTEM), ds.chat("user", JUDGE_TEMPLATE.format(question=p["prompt"], response=response.text, gold=p["answer"]))]
            verdict = await _judged.judge(meter, eval_name=self.name, item_id=item.id, metric="frames_autorater", candidate=candidate,
                                          messages=messages, log_path=self.log_path, record=record)
            decision, reason = parse_decision(verdict["text"])
            verdict.update({"verdict": None if decision is None else ("TRUE" if decision else "FALSE"), "reason": reason})
            if decision is None:
                raise ValueError("裁判的回答里没有 Decision: TRUE/FALSE")
            return meter.result(1.0 if decision else 0.0, record)
        except Exception as exc:
            return meter.error(exc, record)


def make(**options: Any) -> FramesEval:
    return FramesEval(**options)


__all__ = ["FramesEval", "JUDGE_TEMPLATE", "load", "make", "parse_decision"]
