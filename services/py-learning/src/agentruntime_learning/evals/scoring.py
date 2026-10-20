"""评分器（计划 S-08）：结构校验、规则检查、Triage 与形状特征、Verdict 与升级顺序。

失败分维度记录（Failure.dimension）：

- schema：输出不合 /schemas，或 Stage 的 DAG 不合法（必须 100% 通过）。
- rule：由代码判定的规则，不看标注，例如风险上升必须走 full、不可逆动作前必须有 gate。
- triage：Triage 级别与标注不符。
- shape：Stage 的形状特征与标注不符。
- verdict：Verdict 不在可接受的范围内。
- escalation：该升级却原地重试（低于标注的 min_escalation 时还在 repair）。
- error：被测对象调用出错（运行器记录，code = subject_error）。

Failure.message 只描述哪里不对（例如“期望 fast，得到 full”），不引用用例内容，留出集的报告也可以汇总它。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentruntime_learning.evals import schemas, stages
from agentruntime_learning.evals.cases import Checkpoint, TaskCase
from agentruntime_learning.evals.model import Catalog, PlannerOutput, ReviewerOutput, Usage

DIMENSIONS = ("schema", "rule", "triage", "shape", "verdict", "escalation", "error")

# 规则（Failure.code）。
RULE_RISK_RISE_REQUIRES_FULL = "risk_rise_requires_full"
RULE_IRREVERSIBLE_REQUIRES_GATE = "irreversible_requires_gate"
RULE_TOOLS_WITHIN_NODE_CEILING = "tools_within_node_ceiling"
RULE_EFFECT_NOT_BELOW_CATALOG = "effect_not_below_catalog"
RULE_TEMPLATE_IN_CATALOG = "template_in_catalog"
RULE_EFFECT_WITHIN_CEILING = "effect_within_ceiling"
RULE_CHEAP_TRIAGE_NO_STRONG_MODEL = "cheap_triage_no_strong_model"
RULE_LIGHT_REQUIRES_RECIPE_HIT = "light_requires_recipe_hit"
RULE_DONE_FROM_DELIVER = "done_from_deliver"

ESCALATION_ORDER = (
    "none",
    "repair_retry",
    "repair_change_method",
    "replan",
    "replan_change_shape",
    "rollback",
    "escalate",
    "abort",
)
"""附录 B.8 的升级顺序；reroute 与 replan_change_shape 同级。"""

TRIAGE_LEVELS = ("fast", "light", "full", "replan")
# 强档：Tier 的模型档为 strong（附录 H.1，形如 strong@medium）。
STRONG_TIER = "strong"


@dataclass(frozen=True)
class Failure:
    dimension: str
    code: str
    message: str


@dataclass
class DecisionScore:
    """一次决策（一次 plan 或一次 review）的得分。"""

    passed: bool
    failures: list[Failure] = field(default_factory=list)
    cost_usd: float = 0.0
    vendors: frozenset[str] = frozenset()

    @property
    def schema_ok(self) -> bool:
        return not any(f.dimension in ("schema", "error") for f in self.failures)

    def failed(self, dimension: str) -> bool:
        return any(f.dimension == dimension for f in self.failures)


def _score(failures: list[Failure], usage: list[Usage]) -> DecisionScore:
    return DecisionScore(
        passed=not failures,
        failures=failures,
        cost_usd=sum(u.cost_usd for u in usage),
        vendors=frozenset(u.vendor for u in usage if u.vendor),
    )


def _range_text(spec: dict[str, int]) -> str:
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and hi is not None:
        return f"{lo} 到 {hi}" if lo != hi else str(lo)
    return f"至少 {lo}" if lo is not None else f"至多 {hi}"


def _in_range(value: int, spec: dict[str, int]) -> bool:
    return spec.get("min", value) <= value <= spec.get("max", value)


# --- 形状特征 ---


def stage_features(stage: dict[str, Any], catalog: Catalog) -> dict[str, Any]:
    """Stage 的形状特征，键与 case.schema.json 的 shape 相同（keep_nodes 除外，返回 node_ids）。"""
    nodes = stages.nodes(stage)
    levels = stages.levels(stage)
    width: dict[int, int] = {}
    for level in levels.values():
        width[level] = width.get(level, 0) + 1
    writers = [str(n.get("id")) for n in nodes if stages.write_effect(n, catalog) in stages.WRITE_EFFECTS]
    ups = stages.ancestors(stage)
    parallel_writers = any(a != b and a not in ups.get(b, set()) and b not in ups.get(a, set()) for a in writers for b in writers)
    return {
        "nodes": len(nodes),
        "fan_out": max(width.values(), default=0),
        # 显式的 gate 节点，或按 write_irreversible 声明的 Atomic Node（运行时在它前面插入人工审批，附录 F.3）。
        "gate": any(n.get("role") == "gate" or (n.get("kind") == "atomic" and n.get("effect") == "write_irreversible") for n in nodes),
        "human": any(n.get("executor") == "human" for n in nodes),
        "deliver": any(n.get("role") == "deliver" for n in nodes),
        "modes": sorted({str(n["mode"]) for n in nodes if n.get("kind") == "goal" and n.get("mode")}),
        "quality": sorted({q for n in nodes if (q := str((n.get("quality") or {}).get("type", "none"))) != "none"}),
        "single_writer": not parallel_writers,
        "roles": sorted({str(n["role"]) for n in nodes if n.get("role")}),
        "node_ids": sorted(str(n.get("id")) for n in nodes),
    }


def _shape_failures(features: dict[str, Any], shape: dict[str, Any]) -> list[Failure]:
    found: list[Failure] = []
    names = {"nodes": "顶层 Node 数", "fan_out": "并行宽度"}
    for key in ("nodes", "fan_out"):
        if key in shape and not _in_range(features[key], shape[key]):
            found.append(Failure("shape", key, f"{names[key]}期望 {_range_text(shape[key])}，得到 {features[key]}"))
    flags = {"gate": " gate（人工确认或不可逆动作前的审批）", "human": " human 节点", "deliver": " deliver 节点"}
    for key, text in flags.items():
        if key in shape and shape[key] != features[key]:
            found.append(Failure("shape", key, f"期望{'有' if shape[key] else '没有'}{text}，实际{'有' if features[key] else '没有'}"))
    for key, text in (("modes", " Goal Node mode"), ("quality", "质量包装")):
        missing = sorted(set(shape.get(key, [])) - set(features[key]))
        if missing:
            found.append(Failure("shape", key, f"缺少{text}：{'、'.join(missing)}"))
    if shape.get("single_writer") and not features["single_writer"]:
        found.append(Failure("shape", "single_writer", "写操作并行了，应由单一写者完成"))
    present = sorted(set(shape.get("forbidden_roles", [])) & set(features["roles"]))
    if present:
        found.append(Failure("shape", "forbidden_roles", f"本轮不该有 role = {'、'.join(present)} 的 Node（一轮只规划到下一个信息检查点）"))
    redone = sorted(set(shape.get("keep_nodes", [])) & set(features["node_ids"]))
    if redone:
        found.append(Failure("shape", "keep_nodes", f"重做了已验证的 Node：{'、'.join(redone)}"))
    return found


# --- Planner ---


def _ceiling(case: TaskCase) -> str:
    ceilings = [case.planner_input["context"]["effect_ceiling"]]
    state_ceiling = ((case.planner_input.get("task_state") or {}).get("constraints") or {}).get("effect_ceiling")
    if state_ceiling:
        ceilings.append(state_ceiling)
    return min(ceilings, key=stages.rank)


def _rule_failures(case: TaskCase, output: PlannerOutput, stage: dict[str, Any], catalog: Catalog) -> list[Failure]:
    found: list[Failure] = []
    context = case.planner_input["context"]
    expect = case.planner_expect
    level = output.triage_level
    risk, before = expect["risk"], context["risk_before"]
    if stages.rank(risk) > stages.rank(before) and level != "full":
        found.append(Failure("rule", RULE_RISK_RISE_REQUIRES_FULL, f"风险从 {before} 升到 {risk}，Triage 应为 full，得到 {level}"))
    if level in ("fast", "light"):
        strong = sorted({u.tier for u in output.usage if u.tier.split("@", 1)[0] == STRONG_TIER})
        if strong:
            found.append(Failure("rule", RULE_CHEAP_TRIAGE_NO_STRONG_MODEL, f"{level} 不该调用强模型（{'、'.join(strong)}）"))
    if level == "light":
        recipe = (output.task_plan or {}).get("recipe")
        hits = [r for r in context.get("recipes", []) if r["recipe"] == recipe and not r.get("recent_failure")]
        if not hits or context.get("model_profile_updated"):
            found.append(Failure("rule", RULE_LIGHT_REQUIRES_RECIPE_HIT, f"light 要命中 Recipe 缓存里最近没有失败、Model Profile 也没更新的条目，得到 {recipe}"))
    found += [Failure("rule", RULE_TEMPLATE_IN_CATALOG, p) for p in stages.catalog_problems(stage, catalog)]
    ceiling = _ceiling(case)
    irreversible = set(expect.get("irreversible_actions", []))
    irreversible |= {t for t, e in catalog.templates.items() if e.effect == "write_irreversible"}
    irreversible |= {t for t, e in catalog.tools.items() if e == "write_irreversible"}
    for node in stages.nodes(stage):
        nid = node.get("id")
        effect = stages.declared_effect(node, ceiling)
        if stages.rank(effect) > stages.rank(ceiling):
            found.append(Failure("rule", RULE_EFFECT_WITHIN_CEILING, f"{nid} 的 effect {effect} 超过上限 {ceiling}"))
        if node.get("kind") == "atomic":
            template = str(node.get("template"))
            if template in irreversible and node.get("effect") != "write_irreversible":
                found.append(Failure("rule", RULE_IRREVERSIBLE_REQUIRES_GATE, f"{nid} 是不可逆动作，却按 {node.get('effect')} 声明：运行时不会在它前面插入人工审批"))
            elif template in catalog.templates and stages.rank(str(node.get("effect"))) < stages.rank(catalog.templates[template].effect):
                found.append(Failure("rule", RULE_EFFECT_NOT_BELOW_CATALOG, f"{nid} 按 {node.get('effect')} 声明，低于模板的 {catalog.templates[template].effect}"))
        else:
            # tool_loop 里每次调用不可逆工具前都逐次审批（D16），不可逆工具放进 Goal Node 不再违规；
            # 但工具的 effect 高于这个 Node 自己的上限时，运行时在展开前就拒绝整个 Node。
            over = sorted(str(t) for t in node.get("tools") or [] if t in catalog.tools and stages.rank(catalog.tools[t]) > stages.rank(effect))
            if over:
                found.append(Failure("rule", RULE_TOOLS_WITHIN_NODE_CEILING, f"{nid} 的工具 {'、'.join(over)} 高于它自己的 effect 上限 {effect}：运行时会拒绝整个 Node"))
    return found


def score_planner(case: TaskCase, output: PlannerOutput, catalog: Catalog) -> DecisionScore:
    failures: list[Failure] = []
    level = output.triage_level
    if level not in TRIAGE_LEVELS:
        failures.append(Failure("schema", "triage_level", f"Triage 级别 {level!r} 不在 {'、'.join(TRIAGE_LEVELS)} 之中"))
    stage = stages.with_idempotency_keys(output.stage)
    stage_errors = schemas.locations(schemas.validator("stage"), stage)
    failures += [Failure("schema", "stage_schema", f"Stage 不合 stage.schema.json：{e}") for e in stage_errors]
    if not stage_errors:
        failures += [Failure("schema", "stage_dag", f"Stage 的 DAG 不合法：{e}") for e in stages.dag_problems(stage)]
    if output.task_plan is None:
        if level in ("full", "light", "replan"):
            failures.append(Failure("schema", "task_plan_missing", f"{level} 要给出 Task Plan"))
    else:
        plan_errors = schemas.locations(schemas.validator("task_plan"), output.task_plan)
        failures += [Failure("schema", "task_plan_schema", f"Task Plan 不合 task_plan.schema.json：{e}") for e in plan_errors]
        plan_level = (output.task_plan.get("triage") or {}).get("level") if isinstance(output.task_plan.get("triage"), dict) else None
        if not plan_errors and plan_level != level:
            failures.append(Failure("schema", "triage_inconsistent", f"Task Plan 的 Triage 是 {plan_level}，输出的 Triage 是 {level}"))
    expected = case.planner_expect["triage"]
    if level != expected:
        failures.append(Failure("triage", "triage", f"期望 {expected}，得到 {level}"))
    if not any(f.dimension == "schema" and f.code in ("stage_schema", "stage_dag") for f in failures):
        failures += _rule_failures(case, output, stage, catalog)
        failures += _shape_failures(stage_features(stage, catalog), case.planner_expect["shape"])
    return _score(failures, output.usage)


# --- Reviewer ---


def escalation_step(verdict: dict[str, Any]) -> str:
    """Verdict 对应的升级步骤（ESCALATION_ORDER 之一）。"""
    name = verdict.get("verdict")
    if name in ("done", "continue"):
        return "none"
    if name == "repair":
        return "repair_change_method" if (verdict.get("repair") or {}).get("strategy") == "change_method" else "repair_retry"
    if name == "replan":
        return "replan_change_shape" if (verdict.get("replan") or {}).get("change_shape") else "replan"
    if name == "reroute":
        return "replan_change_shape"
    if name in ("rollback", "escalate", "abort"):
        return str(name)
    raise ValueError(f"未知的 Verdict {name!r}")


def score_reviewer(checkpoint: Checkpoint, output: ReviewerOutput) -> DecisionScore:
    verdict = output.verdict
    errors = schemas.locations(schemas.validator("verdict"), verdict)
    if errors:
        return _score([Failure("schema", "verdict_schema", f"Verdict 不合 verdict.schema.json：{e}") for e in errors], output.usage)
    failures: list[Failure] = []
    expect = checkpoint.expect
    name = verdict["verdict"]
    if name not in expect["acceptable"]:
        failures.append(Failure("verdict", "verdict", f"期望 {expect['verdict']}（可接受：{'、'.join(expect['acceptable'])}），得到 {name}"))
    step, floor = escalation_step(verdict), expect["min_escalation"]
    if name == "repair" and ESCALATION_ORDER.index(step) < ESCALATION_ORDER.index(floor):
        failures.append(Failure("escalation", "stuck_retry", f"至少应升级到 {floor}，却在 {step} 原地重试"))
    if name == "done":
        node = verdict["done"]["deliver_node"]
        result = checkpoint.results.get(node)
        if result is None or result.get("role") != "deliver" or result.get("status") != "succeeded":
            failures.append(Failure("rule", RULE_DONE_FROM_DELIVER, f"done 的交付节点 {node} 不是本轮成功的 deliver 节点"))
    return _score(failures, output.usage)
