"""归纳候选 Recipe（附录 J.4“晋升：某任务族的做法连续验证成功后，固化为 Recipe”；T-16 第 3 步）。

- 只看该任务族、没有用 Recipe（完整规划）的 Task。做法签名是经 Planner 规划的各 Round（有 triage_level）的
  Stage 形状里的结构：Node 的 id、kind、template、after、tier、mode；goal、exit_when 的措辞不算。
- 取成功（done）次数最多的签名；不足 min_support 不产出。并列时取签名的字典序最小的。
- 正文取最近一次（task_id 最大）成功 Task 的 Stage 形状。risk 是各 atomic 节点模板 effect 的最大值，
  有 Goal Node 时按 write_irreversible（docs/plan/learning-pipeline.md 第 4 节第 3 条）；查不到模板的 effect 就不产出。
- 正文按 recipe.schema.json 校验，不合格不产出。
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from agentruntime_learning.evals.schemas import problems, validator
from agentruntime_learning.pipeline.traces import TaskTrace

SIGNATURE_KEYS = ("id", "kind", "template", "after", "tier", "mode")
ATOMIC_KEYS = ("id", "kind", "template", "after", "output_schema", "tier", "reasoning", "quality")
GOAL_KEYS = ("id", "kind", "goal", "done_when", "mode", "output_schema", "after", "quality")
EFFECT_ORDER = ("none", "read", "write_reversible", "write_irreversible")

Signature = tuple[str, ...]


@dataclass(frozen=True)
class RecipeCandidate:
    name: str
    document: dict[str, Any]
    signature: Signature
    support: int
    supporting_task_ids: tuple[str, ...]


def recipe_name(task_family: str) -> str:
    return "learned_" + task_family.replace(".", "_")


def _planned_shapes(task: TaskTrace) -> list[dict[str, Any]] | None:
    shapes = [r.shape for r in task.rounds if r.planned]
    if not shapes or any(s is None for s in shapes):
        return None
    return shapes  # type: ignore[return-value]


def signature(task: TaskTrace) -> Signature | None:
    """做法签名：每个经 Planner 的 Round 一项（Node 结构的规范 JSON）。Round 没有形状时为 None。"""
    shapes = _planned_shapes(task)
    if shapes is None:
        return None
    return tuple(
        json.dumps([{k: n[k] for k in SIGNATURE_KEYS if k in n} for n in shape.get("nodes") or []], ensure_ascii=False, sort_keys=True)
        for shape in shapes
    )


def eligible(tasks: Iterable[TaskTrace], task_family: str) -> list[TaskTrace]:
    """该任务族、完整规划（没有用 Recipe）、有形状的 Task。"""
    return [t for t in tasks if t.task_family == task_family and not t.recipe and signature(t) is not None]


def best_route(tasks: Iterable[TaskTrace], task_family: str, *, min_support: int) -> tuple[Signature, list[TaskTrace]] | None:
    """成功次数最多的签名与支持它的成功 Task（按 task_id 排序）；不足 min_support 时为 None。"""
    done = [t for t in eligible(tasks, task_family) if t.status == "done"]
    counts = Counter(signature(t) for t in done)
    if not counts:
        return None
    best = min(counts, key=lambda s: (-counts[s], json.dumps(s, ensure_ascii=False)))
    if counts[best] < min_support:
        return None
    return best, sorted((t for t in done if signature(t) == best), key=lambda t: t.task_id)  # type: ignore[arg-type]


def template_refs(shapes: Iterable[Mapping[str, Any]]) -> list[str]:
    return sorted({str(n["template"]) for s in shapes for n in s.get("nodes") or [] if n.get("kind") == "atomic" and n.get("template")})


def _node(node: Mapping[str, Any]) -> dict[str, Any]:
    keys = GOAL_KEYS if node.get("kind") == "goal" else ATOMIC_KEYS
    return {k: node[k] for k in keys if k in node}


def _risk(shapes: list[dict[str, Any]], templates: Mapping[str, str]) -> str | None:
    effects = []
    for shape in shapes:
        for node in shape.get("nodes") or []:
            if node.get("kind") == "goal":
                effects.append("write_irreversible")
                continue
            effect = templates.get(str(node.get("template")))
            if effect not in EFFECT_ORDER:
                return None
            effects.append(effect)
    return max(effects, key=EFFECT_ORDER.index) if effects else None


def mine_recipe(tasks: Iterable[TaskTrace], task_family: str, *, templates: Mapping[str, str], min_support: int = 5) -> RecipeCandidate | None:
    """templates：模板引用（name@vN）→ effect。"""
    route = best_route(list(tasks), task_family, min_support=min_support)
    if route is None:
        return None
    sig, supporting = route
    shapes = _planned_shapes(supporting[-1])
    assert shapes is not None
    risk = _risk(shapes, templates)
    if risk is None:
        return None
    stages = [{"goal": s.get("goal"), "exit_when": s.get("exit_when"), "nodes": [_node(n) for n in s.get("nodes") or []]} for s in shapes]
    document = {
        "task_family": task_family,
        "description": f"从 {len(supporting)} 次成功的 Task 归纳的做法（Learning Pipeline）：" + " → ".join(str(s["goal"]) for s in stages),
        "risk": risk,
        "typical_cost_usd": round(statistics.median(t.cost_usd for t in supporting), 6),
        "stages": stages,
    }
    if problems(validator("recipe"), document):
        return None
    return RecipeCandidate(
        name=recipe_name(task_family),
        document=document,
        signature=sig,
        support=len(supporting),
        supporting_task_ids=tuple(t.task_id for t in supporting),
    )
