"""S-10 的 Reviewer 判定点当能力矩阵的题（docs/plan/capability-matrix-evals.md 第 3 节，role judge）。

一个判定点一道题：Reviewer 专用样本（checkpoints="samples" 时只用它们），加上 Task 用例里每个 Round 结束时的判定点
（默认 "all"：S-10 的 24 条样本凑不够每格 40 道，Round 的判定点同样有人标的 Verdict）。Reviewer 用 Worker 的实现
（evals.worker_subject.WorkerSubject，judge 角色的调用被钉在候选上），一道题通过 = score_reviewer 通过
（Verdict 在可接受的范围里、不该原地重试时没有原地重试、done 指向本轮成功的 deliver 节点、结构合格）。

同一条 Task 用例的几个 Round 不是完全独立的题；bootstrap 按题重抽样时会略微低估区间，报告里照实写题数。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from agentruntime_worker.executor.model_gateway import ModelGateway

from agentruntime_learning.capability.base import Item, ItemResult
from agentruntime_learning.capability.evals.s09 import failures, pick, select_split
from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET, Checkpoint, load_eval_set
from agentruntime_learning.evals.model import ReviewerInput
from agentruntime_learning.evals.scoring import score_reviewer
from agentruntime_learning.evals.worker_subject import WorkerSubject, eval_recipes

CHECKPOINTS = ("all", "samples")


class S10Judge:
    name = "s10"
    role = "judge"
    # S-10 实测约 0.017 美元/题（含 Reviewer，2026-11-02）：按强档的单价约 6k 输入、500 输出。
    est_tokens = (6000, 500)
    est_other_usd = 0.0

    def __init__(self, eval_set: str | Path | None = None, split: str = "all", checkpoints: str = "all") -> None:
        if checkpoints not in CHECKPOINTS:
            raise ValueError(f"checkpoints 只能是 {'、'.join(CHECKPOINTS)}，得到 {checkpoints!r}")
        self.eval_set = load_eval_set(Path(eval_set) if eval_set else DEFAULT_EVAL_SET)
        self.version = self.eval_set.version[:12]
        self.recipes = eval_recipes(self.eval_set)
        selected = select_split(self.eval_set, split)
        self.checkpoints: dict[str, tuple[str, Checkpoint]] = {s.id: (s.split, s.checkpoint) for s in selected.reviewer_samples}
        if checkpoints == "all":
            for case in selected.task_cases:
                for i, cp in enumerate(case.rounds, start=1):
                    self.checkpoints[f"{case.id}#round{i}"] = (case.split, cp)

    def items(self, n: int, seed: int) -> list[Item]:
        return [Item(id=i, payload={"split": self.checkpoints[i][0]}) for i in pick(list(self.checkpoints), n, seed)]

    async def run_item(self, item: Item, gateway: ModelGateway, *, candidate: str) -> ItemResult:
        split, cp = self.checkpoints[item.id]
        subject = WorkerSubject(gateway, recipes=self.recipes)
        output = await subject.review(
            ReviewerInput(
                task_state=copy.deepcopy(cp.task_state),
                stage=copy.deepcopy(cp.stage),
                results=copy.deepcopy(cp.results),
                history=copy.deepcopy(cp.history),
            )
        )
        score = score_reviewer(cp, output)
        record: dict[str, Any] = {
            "split": split,
            "verdict_expected": cp.expect["verdict"],
            "acceptable": list(cp.expect["acceptable"]),
            "verdict": output.verdict.get("verdict") if isinstance(output.verdict, dict) else None,
            "fallback": output.fallback,
            "failures": failures(score),
            "model_call_failures": dict(subject.model_call_failures),
        }
        return ItemResult(item.id, score=1.0 if score.passed else 0.0, cost_usd=score.cost_usd, record=record)


def make(eval_set: str | None = None, split: str = "all", checkpoints: str = "all") -> S10Judge:
    return S10Judge(eval_set, split, checkpoints)
