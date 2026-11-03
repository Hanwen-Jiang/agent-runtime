"""SimpleQA（docs/plan/capability-matrix-evals.md 第 3 节，role deliver）：短事实题，测“答得对、不知道时不编”；裁判按官方的评分提示判。

数据：OpenAI simple-evals 的 `simple_qa_test_set.csv`（4326 题；MIT），核对 sha256 后缓存在 `<AR_EVAL_DATA_DIR>/simpleqa/`。
CSV 没有题号，题号是钉死的这份文件里的行号（`simpleqa-<行>`）。

流程照官方的 `simpleqa_eval.py`（simple-evals 652c89d）：题目原样作为唯一的用户消息给被测模型；裁判看
`simpleqa_grader.txt`（官方 GRADER_TEMPLATE 的原文，按 sha256 核对）填进题目、标准答案与回答，只回一个字母：
A = CORRECT、B = INCORRECT、C = NOT_ATTEMPTED（找不到字母时按 C，同官方）。

逐题分数（2026-11-03 人定）：CORRECT 记 1，NOT_ATTEMPTED（老实说不知道）记 `abstain_credit`（默认 0.25），INCORRECT（编错）记 0。
deliver 要测“不编造”，官方的逐题规则（只有答对记 1）把不知道与瞎编算成一样，等于鼓励瞎编；只会说不知道的模型最多 0.25，排不上前面。
`abstain_credit=0` 回到官方规则；用的是哪种写在 `version` 里（`:abstain=<分>`），不同的口径不能混在一次运行里续跑。
三种结论都写进 record（`grade`）；能力矩阵的 scores.json 另附官方口径（`official_metrics`：答对率、答了的里答对的比例、F 分数、
答了的里答错的比例），便于与公开榜单对照。
裁判的调用、记录与同家冲突见 `_judged`。
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from pathlib import Path
from collections.abc import Sequence
from typing import Any

from agentruntime_learning.capability.base import Item, ItemResult
from agentruntime_learning.capability.evals import _datasets as ds
from agentruntime_learning.capability.evals import _judged
from agentruntime_worker.executor.model_gateway import ModelGateway

NAME = "simpleqa"
ROLE = "deliver"
CSV_URL = "https://openaipublic.blob.core.windows.net/simple-evals/simple_qa_test_set.csv"
CSV_SHA256 = "feee3f7e7db3617e94e8fcf1977b756ec420ef8568f4e0fcbbe0e92e9d5fc032"
SIMPLE_EVALS_COMMIT = "652c89d0ca9df547706735883097e9537d40dc47"
ABSTAIN_CREDIT = 0.25
GRADER_SHA256 = "063c04ea798f91c4706c050b66a721afc77a458b12d8f0d72286d4b0eee8a3ee"
GRADER_TEMPLATE = (Path(__file__).with_name("simpleqa_grader.txt")).read_text("utf-8")
GRADES = {"A": "CORRECT", "B": "INCORRECT", "C": "NOT_ATTEMPTED"}

if hashlib.sha256(GRADER_TEMPLATE.encode()).hexdigest() != GRADER_SHA256:  # pragma: no cover — 防止编辑器动了原文（行尾空格等）
    raise RuntimeError("simpleqa_grader.txt 与官方的 GRADER_TEMPLATE 不一致")


def parse_grade(text: str) -> str:
    m = re.search(r"(A|B|C)", text)
    return m.group(0) if m else "C"


def load(source: Path | None = None) -> list[dict[str, Any]]:
    path = Path(source) if source else ds.download(CSV_URL, ds.data_dir() / NAME / "simple_qa_test_set.csv", sha256=CSV_SHA256)
    rows = csv.DictReader(io.StringIO(path.read_text("utf-8")))
    return [{"id": f"simpleqa-{i}", "problem": r["problem"], "answer": r["answer"], "metadata": r.get("metadata", "")} for i, r in enumerate(rows)]


class SimpleQAEval:
    name = NAME
    role = ROLE
    uses_judge = True  # 裁判调用经 RecordingJudgeGateway（见 _judged）
    est_tokens = (50, 150)  # 被测作答（不含思考）
    est_other_usd = 0.006  # Opus 裁判约 1.5k 输入

    def __init__(self, *, source: Path | None = None, judge_log: Path | str | None = None, max_output_tokens: int = 2048,
                 abstain_credit: float = ABSTAIN_CREDIT) -> None:
        if not 0 <= float(abstain_credit) < 1:
            raise ValueError("abstain_credit 要在 [0, 1) 里：不回答不能与答对一样")
        self.abstain_credit = float(abstain_credit)
        self.version = f"csv@{CSV_SHA256[:7]}:abstain={self.abstain_credit:g}"
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
        record: dict[str, Any] = {"question": p["problem"], "gold": p["answer"], "metadata": p.get("metadata", ""), "candidate": candidate,
                                  "same_vendor": _judged.same_vendor(candidate)}
        try:
            response = await meter.generate(role=ROLE, node="simpleqa.answer", messages=[ds.chat("user", p["problem"])],
                                            max_output_tokens=self.max_output_tokens)
            record["answer"] = response.text
            grader = GRADER_TEMPLATE.format(question=p["problem"], target=p["answer"], predicted_answer=response.text)
            verdict = await _judged.judge(meter, eval_name=self.name, item_id=item.id, metric="simpleqa_grade", candidate=candidate,
                                          messages=[ds.chat("user", grader)], log_path=self.log_path, record=record)
            letter = parse_grade(verdict["text"])
            verdict.update({"verdict": GRADES[letter], "reason": verdict["text"]})
            record["grade"] = GRADES[letter]
            return meter.result({"A": 1.0, "B": 0.0, "C": self.abstain_credit}[letter], record)
        except Exception as exc:
            return meter.error(exc, record)


def official_metrics(grades: Sequence[str]) -> dict[str, Any]:
    """官方口径（simple-evals 的 simpleqa_eval.py）：答对率、答了的里答对的比例与两者的调和平均（F 分数）；另给答了的里答错的比例。"""
    n = len(grades)
    correct, incorrect, not_attempted = (sum(1 for g in grades if g == k) for k in ("CORRECT", "INCORRECT", "NOT_ATTEMPTED"))
    attempted = correct + incorrect
    rate = correct / n if n else 0.0
    given = correct / attempted if attempted else 0.0
    return {
        "n": n,
        "correct": rate,
        "incorrect": incorrect / n if n else 0.0,
        "not_attempted": not_attempted / n if n else 0.0,
        "correct_given_attempted": given,
        "incorrect_given_attempted": incorrect / attempted if attempted else 0.0,
        "f_score": 2 * rate * given / (rate + given) if rate + given else 0.0,
    }


def make(**options: Any) -> SimpleQAEval:
    return SimpleQAEval(**options)


__all__ = ["ABSTAIN_CREDIT", "GRADER_TEMPLATE", "SimpleQAEval", "load", "make", "official_metrics", "parse_grade"]
