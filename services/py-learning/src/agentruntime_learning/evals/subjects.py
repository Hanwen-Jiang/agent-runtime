"""内置的被测对象（计划 S-08）。

- baseline：永远返回 full + 单个 Node（一个 tool_loop 的 Goal Node）；Reviewer 有失败就原地重试，否则判 done。
  用来验证运行器能给出可解释的低分（S-08 验收）。
- oracle：按标注里人确认过的参照输出作答，用来自检评分器与标注是否一致（应得满分）。
- 其他：<模块>:<工厂函数>，工厂函数返回 Subject；S-09、S-10 的 Planner 与 Reviewer 由此接入。
"""

from __future__ import annotations

import copy
import importlib
import json
from typing import Any

from agentruntime_learning.evals.cases import Checkpoint, EvalSet
from agentruntime_learning.evals.model import PlannerInput, PlannerOutput, ReviewerInput, ReviewerOutput, Subject


class BaselineSubject:
    name = "baseline"

    async def plan(self, inp: PlannerInput) -> PlannerOutput:
        seq = len((inp.task_state or {}).get("rounds", [])) + 1
        node = {
            "id": "do_task",
            "kind": "goal",
            "goal": inp.message,
            "done_when": "用户的请求完成",
            "mode": "tool_loop",
            "output_schema": {"type": "object"},
        }
        stage = {"seq": seq, "goal": "完成用户的请求", "exit_when": "do_task 给出结果", "nodes": [node]}
        return PlannerOutput(triage_level="full", stage=stage, task_plan={"triage": {"level": "full"}, "horizon": "full_dag"})

    async def review(self, inp: ReviewerInput) -> ReviewerOutput:
        failed = sorted(nid for nid, r in inp.results.items() if r.get("status") not in ("succeeded", "skipped"))
        delivered = sorted(nid for nid, r in inp.results.items() if r.get("role") == "deliver" and r.get("status") == "succeeded")
        if failed:
            verdict: dict[str, Any] = {"verdict": "repair", "reason": "有 Node 失败，带上错误重试", "repair": {"nodes": failed, "strategy": "retry_with_error"}}
        elif delivered:
            verdict = {"verdict": "done", "reason": "有交付结果", "done": {"deliver_node": delivered[0]}}
        else:
            verdict = {"verdict": "continue", "reason": "还没有交付结果"}
        return ReviewerOutput(verdict=verdict)


def _key(*parts: Any) -> str:
    return json.dumps(parts, ensure_ascii=False, sort_keys=True)


def _review_key(task_state: Any, stage: Any, results: Any, history: Any) -> str:
    return _key(task_state, stage, results, history)


class OracleSubject:
    """按输入内容（不是用例 id）找到对应的参照输出。"""

    name = "oracle"

    def __init__(self, eval_set: EvalSet) -> None:
        self.eval_set = eval_set
        self._plans: dict[str, PlannerOutput] = {}
        self._reviews: dict[str, dict[str, Any]] = {}
        checkpoints: list[Checkpoint] = [s.checkpoint for s in eval_set.reviewer_samples]
        for case in eval_set.task_cases:
            inp = case.planner_input
            expect = case.planner_expect
            key = _key(inp["message"], inp.get("task_state"), inp.get("attachments", []), inp["context"])
            self._plans[key] = PlannerOutput(
                triage_level=expect["triage"],
                stage=expect["reference"]["stage"],
                task_plan=expect["reference"].get("task_plan"),
            )
            checkpoints += case.rounds
        for cp in checkpoints:
            self._reviews[_review_key(cp.task_state, cp.stage, cp.results, cp.history)] = cp.expect["reference"]

    async def plan(self, inp: PlannerInput) -> PlannerOutput:
        out = self._plans[_key(inp.message, inp.task_state, inp.attachments, inp.context)]
        return PlannerOutput(triage_level=out.triage_level, stage=copy.deepcopy(out.stage), task_plan=copy.deepcopy(out.task_plan))

    async def review(self, inp: ReviewerInput) -> ReviewerOutput:
        return ReviewerOutput(verdict=copy.deepcopy(self._reviews[_review_key(inp.task_state, inp.stage, inp.results, inp.history)]))


def load_subject(spec: str, eval_set: EvalSet) -> Subject:
    """baseline、oracle，或 <模块>:<工厂函数>（工厂函数不带参数，返回 Subject）。"""
    if spec == "baseline":
        return BaselineSubject()
    if spec == "oracle":
        return OracleSubject(eval_set)
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(f"未知的被测对象 {spec!r}：应为 baseline、oracle 或 <模块>:<工厂函数>")
    try:
        factory = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"找不到被测对象 {spec!r}：{exc}") from exc
    subject = factory()
    if not all(hasattr(subject, a) for a in ("name", "plan", "review")):
        raise ValueError(f"{spec} 返回的对象不是 Subject（要有 name、plan、review）")
    return subject
