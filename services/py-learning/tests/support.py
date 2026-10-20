"""py-learning 测试共用：在临时目录里搭一个小评测集，以及一个按脚本作答的被测对象。fixture 在 conftest.py。"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any


from agentruntime_learning.evals.model import PlannerInput, PlannerOutput, ReviewerInput, ReviewerOutput, Usage

REPO_ROOT = Path(__file__).resolve().parents[3]
TASK_ID = "tsk_01J" + "0" * 22 + "T"
SESSION_ID = "ses_01J" + "0" * 22 + "S"

CATALOG: dict[str, Any] = {
    "templates": {
        "sql_query@v1": {"executor": "program", "effect": "read"},
        "summarize@v1": {"executor": "model", "effect": "none"},
        "answer@v1": {"executor": "model", "effect": "none"},
        "confirm_scope@v1": {"executor": "human", "effect": "none"},
        "pay_order@v1": {"executor": "program", "effect": "write_irreversible"},
        "save_draft@v1": {"executor": "program", "effect": "write_reversible"},
        "check_numbers@v1": {"executor": "program", "effect": "none"},
    },
    "recipes": {"refund_analysis@v3": {"task_family": "analytics.attribution"}},
    "tools": {"web_search": "read", "flight_search": "read", "card_charge": "write_irreversible", "repo_edit": "write_reversible"},
}


def atomic(node_id: str, template: str, *, executor: str = "program", effect: str = "read", role: str = "fetch", after: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"id": node_id, "kind": "atomic", "template": template, "executor": executor, "effect": effect, "role": role}
    if executor == "model":
        node["output"] = "generate"
    if effect == "write_irreversible":
        node["retry"] = {"max": 0}
    if after:
        node["after"] = after
    node.update(extra)
    return node


def goal(node_id: str, *, mode: str = "tool_loop", after: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"id": node_id, "kind": "goal", "goal": f"完成 {node_id}", "done_when": "给出结果", "mode": mode, "output_schema": {"type": "object"}}
    if after:
        node["after"] = after
    node.update(extra)
    return node


def stage(*nodes: dict[str, Any], seq: int = 1, goal_text: str = "完成本轮") -> dict[str, Any]:
    return {"seq": seq, "goal": goal_text, "exit_when": "本轮的 Node 都有结果", "nodes": list(nodes)}


def answer_stage(seq: int = 1) -> dict[str, Any]:
    return stage(atomic("answer", "answer@v1", executor="model", effect="none", role="deliver"), seq=seq)


def task_state(**extra: Any) -> dict[str, Any]:
    state: dict[str, Any] = {"task_id": TASK_ID, "session_id": SESSION_ID, "goal": "回答用户的问题", "done_when": "给出可用的回答"}
    state.update(extra)
    return state


def verdict(name: str, **extra: Any) -> dict[str, Any]:
    details: dict[str, Any] = {
        "done": {"done": {"deliver_node": "answer"}},
        "repair": {"repair": {"nodes": ["answer"], "strategy": "retry_with_error"}},
        "rollback": {"rollback": {"checkpoint": "round_1"}},
        "reroute": {"reroute": {"recipe": "refund_analysis@v3"}},
        "escalate": {"escalate": {"target": "human", "question": "按哪个范围继续？", "options": ["continue_as_is", "replan_with_guidance", "stop_and_wrap_up"]}},
        "abort": {"abort": {"completed": [], "not_completed": ["交付"]}},
    }.get(name, {})
    value: dict[str, Any] = {"verdict": name, "reason": f"判定 {name}", **details}
    value.update(extra)
    return value


def checkpoint(name: str = "done", *, acceptable: list[str] | None = None, min_escalation: str = "none", stage_: dict[str, Any] | None = None, results: dict[str, Any] | None = None, history: list[dict[str, Any]] | None = None, reference: dict[str, Any] | None = None, state: dict[str, Any] | None = None) -> dict[str, Any]:
    cp: dict[str, Any] = {
        "task_state": state or task_state(),
        "stage": stage_ or answer_stage(),
        "results": results if results is not None else {"answer": {"role": "deliver", "status": "succeeded", "result": {"text": "好的"}}},
        "expect": {"verdict": name, "acceptable": acceptable or [name], "min_escalation": min_escalation, "reference": reference or verdict(name)},
    }
    if history:
        cp["history"] = history
    return cp


def task_case(
    case_id: str = "t_answer",
    *,
    split: str = "dev",
    category: str = "general",
    triage: str = "full",
    risk: str = "none",
    risk_before: str = "read",
    ceiling: str = "write_irreversible",
    shape: dict[str, Any] | None = None,
    reference_stage: dict[str, Any] | None = None,
    reference_plan: dict[str, Any] | None = None,
    recipes: list[dict[str, Any]] | None = None,
    recipe: str | None = None,
    irreversible: list[str] | None = None,
    rounds: list[dict[str, Any]] | None = None,
    review: dict[str, Any] | None = None,
    message: str = "上季度退款率为什么上升？",
    state: dict[str, Any] | None = None,
    estimated_cost: float | None = None,
    rubric: list[str] | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {"risk_before": risk_before, "effect_ceiling": ceiling}
    if recipes is not None:
        context["recipes"] = recipes
    planner_input: dict[str, Any] = {"message": message, "context": context}
    if state is not None:
        planner_input["task_state"] = state
    if reference_plan is None and triage != "fast":
        reference_plan = {"triage": {"level": triage}, "horizon": "per_stage"}
        if triage == "light":
            reference_plan["recipe"] = recipe
    reference: dict[str, Any] = {"stage": reference_stage or answer_stage()}
    if reference_plan is not None:
        reference["task_plan"] = reference_plan
    expect: dict[str, Any] = {"triage": triage, "risk": risk, "shape": shape or {}, "reference": reference}
    if recipe is not None:
        expect["recipe"] = recipe
    if irreversible:
        expect["irreversible_actions"] = irreversible
    if estimated_cost is not None:
        expect["estimated_task_cost_usd"] = estimated_cost
    case: dict[str, Any] = {
        "kind": "task",
        "id": case_id,
        "split": split,
        "category": category,
        "review": review or {"status": "draft"},
        "description": f"用例 {case_id}",
        "planner": {"input": planner_input, "expect": expect},
        "rounds": rounds or [checkpoint()],
    }
    if rubric:
        case["rubric"] = rubric
    return case


def reviewer_sample(sample_id: str = "r_done", *, split: str = "dev", symptom: str | None = None, review: dict[str, Any] | None = None, **checkpoint_args: Any) -> dict[str, Any]:
    sample: dict[str, Any] = {
        "kind": "reviewer",
        "id": sample_id,
        "split": split,
        "review": review or {"status": "draft"},
        "description": f"样本 {sample_id}",
        "checkpoint": checkpoint(**checkpoint_args),
    }
    if symptom:
        sample["symptom"] = symptom
    return sample


def write_eval_set(root: Path, task_cases: list[dict[str, Any]], samples: list[dict[str, Any]] | None = None, catalog: dict[str, Any] | None = None) -> Path:
    (root / "task_cases").mkdir(parents=True, exist_ok=True)
    (root / "reviewer_samples").mkdir(parents=True, exist_ok=True)
    (root / "catalog.json").write_text(json.dumps(catalog or CATALOG, ensure_ascii=False, indent=2), "utf-8")
    for case in task_cases:
        (root / "task_cases" / f"{case['id']}.json").write_text(json.dumps(case, ensure_ascii=False, indent=2), "utf-8")
    for sample in samples or []:
        (root / "reviewer_samples" / f"{sample['id']}.json").write_text(json.dumps(sample, ensure_ascii=False, indent=2), "utf-8")
    return root


def make_set_factory(tmp_path: Path) -> Callable[..., Path]:
    """make_set(task_cases, samples=None, catalog=None) → 评测集目录。"""
    counter = 0

    def make(task_cases: list[dict[str, Any]], samples: list[dict[str, Any]] | None = None, catalog: dict[str, Any] | None = None) -> Path:
        nonlocal counter
        counter += 1
        return write_eval_set(tmp_path / f"set{counter}", task_cases, samples, catalog)

    return make


class ScriptedSubject:
    """按脚本作答的被测对象：plan_fn、review_fn 接收输入返回输出；记录收到的每个输入。"""

    def __init__(
        self,
        name: str = "scripted",
        plan_fn: Callable[[PlannerInput], PlannerOutput] | None = None,
        review_fn: Callable[[ReviewerInput], ReviewerOutput] | None = None,
    ) -> None:
        self.name = name
        self.plan_fn = plan_fn or (lambda inp: PlannerOutput(triage_level="full", stage=answer_stage(), task_plan={"triage": {"level": "full"}, "horizon": "per_stage"}))
        self.review_fn = review_fn or (lambda inp: ReviewerOutput(verdict=verdict("done")))
        self.plan_inputs: list[PlannerInput] = []
        self.review_inputs: list[ReviewerInput] = []

    async def plan(self, inp: PlannerInput) -> PlannerOutput:
        self.plan_inputs.append(copy.deepcopy(inp))
        return self.plan_fn(inp)

    async def review(self, inp: ReviewerInput) -> ReviewerOutput:
        self.review_inputs.append(copy.deepcopy(inp))
        return self.review_fn(inp)


def usage(cost: float, *, tier: str = "mid@low", vendor: str = "vendor_a") -> Usage:
    return Usage(tier=tier, vendor=vendor, cost_usd=cost)
