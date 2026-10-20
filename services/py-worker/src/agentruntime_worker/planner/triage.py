"""分诊与 Triage（设计书 3.2.2；附录 C.1、C.2）。

分类器（小模型）只给信号；级别由 decide 按规则判定：

1. 上一轮 Reviewer 判 replan → replan。
2. 没有分类信号（分类器出错或输出不合格）→ full。
3. 给运行中的 Task 加约束 → replan。
4. 预计的风险（分类信号的 effect 与命中 Recipe 的风险取高）高于本 Session 已授权的等级 → full。
5. 续改、闲聊、简单问答、关于系统本身的问题，且不需要任何 effect → fast。
6. 新 Task：命中 Recipe 缓存且通过差量检查（Recipe 最近没失败、Model Profile 没更新、置信度够、剩余预算够典型成本，
   涉及不可逆动作时参数能从指令里明确得出）→ light；否则 full。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from agentruntime_worker.planner.context import PlanningContext, RecipeEntry
from agentruntime_worker.workflows import stage_plan as sp

MESSAGE_KINDS = ("follow_up", "constraint", "new_task", "question", "chitchat", "system_question")
"""分诊的消息类别：续改上一个产出、给运行中的 Task 加约束、新 Task、简单问答、闲聊、关于系统本身的问题。"""

SIGNALS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["kind", "confidence", "effect", "irreversible_params_explicit"],
    "properties": {
        "kind": {"enum": list(MESSAGE_KINDS)},
        "task_family": {"type": ["string", "null"], "pattern": "^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "effect": {"enum": ["none", "read", "write_reversible", "write_irreversible"]},
        "irreversible_params_explicit": {"type": "boolean"},
    },
    "additionalProperties": False,
}
"""分类器的结构化输出。"""


@dataclass(frozen=True)
class Signals:
    kind: str
    task_family: str | None
    confidence: float
    effect: str
    """完成这条消息预计需要的最高 effect。"""
    irreversible_params_explicit: bool
    """不可逆动作的对象与参数（金额、收件人、日期等）能否从这次的指令里明确得出（附录 C.2）。"""


@dataclass(frozen=True)
class TriageDecision:
    level: str
    kind: str
    task_family: str | None = None
    confidence: float = 0.0
    recipe: RecipeEntry | None = None
    reasons: list[str] = field(default_factory=list)
    """判定依据，写入 trace；不传给下游。"""


FAST_KINDS = ("follow_up", "chitchat", "question", "system_question")
"""不开新规划的消息类别：续改上一个产出、闲聊、简单问答、关于系统本身的问题。"""

_SIGNALS_VALIDATOR = Draft202012Validator(SIGNALS_SCHEMA)


def parse_signals(value: Any) -> Signals | None:
    """分类器的结构化输出；不合 SIGNALS_SCHEMA 时返回 None（按 full 处理）。"""
    if not isinstance(value, dict) or any(True for _ in _SIGNALS_VALIDATOR.iter_errors(value)):
        return None
    return Signals(
        kind=value["kind"],
        task_family=value.get("task_family"),
        confidence=float(value["confidence"]),
        effect=value["effect"],
        irreversible_params_explicit=bool(value["irreversible_params_explicit"]),
    )


def _rank(effect: str) -> int:
    return sp.effect_rank(effect)


def decide(
    signals: Signals | None,
    *,
    context: PlanningContext,
    last_round: dict[str, Any] | None,
    confidence_threshold: float,
) -> TriageDecision:
    """按附录 C.1、C.2 的规则判定 Triage 级别。纯函数。"""
    verdict = ((last_round or {}).get("verdict") or {}).get("verdict")
    if verdict == "replan":
        return TriageDecision(level="replan", kind=signals.kind if signals else "constraint", reasons=["上一轮 Reviewer 判定 replan，只重做剩余部分"])
    if signals is None:
        return TriageDecision(level="full", kind="new_task", reasons=["分诊没有拿到合格的信号，按 full 规划"])
    base = {"kind": signals.kind, "task_family": signals.task_family, "confidence": signals.confidence}
    if signals.kind == "constraint" and last_round is not None:
        return TriageDecision(level="replan", **base, reasons=["给运行中的 Task 加约束，重新规划剩余部分"])
    recipe = next((r for r in context.recipes if signals.task_family and r.task_family == signals.task_family), None)
    estimated = max([signals.effect, recipe.risk if recipe else "none"], key=_rank)
    if _rank(estimated) > _rank(context.risk_authorized):
        return TriageDecision(level="full", **base, recipe=recipe, reasons=[f"预计的风险 {estimated} 高于本 Session 已授权的 {context.risk_authorized}"])
    if signals.kind in FAST_KINDS and signals.effect == "none":
        return TriageDecision(level="fast", **base, reasons=[f"{signals.kind}：不需要任何 effect，单次调用即可"])
    if recipe is None:
        return TriageDecision(level="full", **base, reasons=["没有命中 Recipe 缓存"])
    checks = [
        (recipe.recent_failure, "缓存的 Recipe 最近失败过"),
        (context.model_profile_updated, "相关 Model Profile 已更新"),
        (signals.confidence < confidence_threshold, f"分诊置信度 {signals.confidence:.2f} 低于阈值 {confidence_threshold:.2f}"),
        (
            recipe.typical_cost_usd is not None
            and context.session_budget_remaining_usd is not None
            and context.session_budget_remaining_usd < recipe.typical_cost_usd,
            "Session 剩余预算不够这个任务族的典型成本",
        ),
        (
            "write_irreversible" in (recipe.risk, signals.effect) and not signals.irreversible_params_explicit,
            "要做不可逆动作，但它的对象或参数不能从这次的指令里明确得出",
        ),
    ]
    failed = [why for hit, why in checks if hit]
    if failed:
        return TriageDecision(level="full", **base, recipe=recipe, reasons=failed)
    return TriageDecision(level="light", **base, recipe=recipe, reasons=[f"命中 Recipe {recipe.recipe}，差量检查通过"])
