"""Planner 输出的校验与规整（计划 S-09 难点“结构化输出”“不能编造模板”）。

规整（normalize）由代码完成：Stage 的 seq 定为本轮序号；写操作的幂等键按 tenant_id、task_id、node_id 与输入重新生成
（附录 D.4，模型写的不算数）；不在 Catalog 可用 Tier 里的 tier（Node 的与 Task Plan 的 tiers）去掉，由 Model Gateway
按 role 选默认 Tier（附录 H.3），不为它重问。校验（problems）不过就带着问题重问模型：

- Stage 与 Task Plan 合 /schemas；Stage 的 DAG 合法（复用 Stage Runner 的检查）。
- Node Template、Recipe、工具（含 quality 的 judge 与 scorer）都在 Node Catalog 里。
- effect 不超过上限；Atomic Node 的 effect 不低于模板的 effect；Goal Node 所列工具不高于它自己的 effect 上限。
- 担任 fetch 或 act 的 Atomic Node 不能由 model 执行（节点或模板的 executor 是 model 都算）：model 节点没有工具，读文件、跑命令、
  改代码都做不了，只会编出结果（2026-10-28 真模型实验第 3.1 条）。这些步骤要用 program 模板或带 tools 的 tool_loop Goal Node。
- replan：Reviewer 保留的 Node 不得在新 Stage 里重做。

这里的检查与 S-08 评分器各写各的，评分器才算独立的核对。
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema_specifications import REGISTRY as SPECIFICATIONS
from referencing import Registry, Resource

from agentruntime_worker.executor import keys
from agentruntime_worker.planner.context import PlanningContext
from agentruntime_worker.workflows import stage_plan as sp
from agentruntime_worker.workflows import plan as plan_steps

# /schemas 随仓库发布；Worker 镜像里放在同样的相对位置。
SCHEMAS_DIR = Path(__file__).resolve().parents[5] / "schemas"
# 顶层 Node 数的上限，与 StagePolicy 的默认值相同。
MAX_NODES = 200
# 要从外部取数或做动作的 role：model 执行不了。
TOOL_ROLES = ("fetch", "act")


@cache
def _registry() -> Registry:
    resources = []
    for path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
        schema = json.loads(path.read_text("utf-8"))
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return SPECIFICATIONS.with_resources(resources)


@cache
def schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS_DIR / f"{name}.schema.json").read_text("utf-8"))


@cache
def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(schema(name), registry=_registry(), format_checker=Draft202012Validator.FORMAT_CHECKER)


def schema_problems(name: str, value: Any) -> list[str]:
    errors = sorted(validator(name).iter_errors(value), key=lambda e: [str(p) for p in e.absolute_path])
    return [f"{name} 的 {'/'.join(str(p) for p in e.absolute_path) or '（根）'}：{e.message}" for e in errors]


def nodes(stage: Any) -> list[dict[str, Any]]:
    if not isinstance(stage, dict) or not isinstance(stage.get("nodes"), list):
        return []
    return [n for n in stage["nodes"] if isinstance(n, dict)]


def normalize_stage(stage: Any, *, round_seq: int, tenant_id: str, task_id: str) -> Any:
    """定下 seq、重新生成写操作的幂等键；不是 dict 时原样返回，交给 problems 报错。"""
    if not isinstance(stage, dict):
        return stage
    out = copy.deepcopy(stage)
    out["seq"] = round_seq
    for node in nodes(out):
        if node.get("kind") == "atomic" and node.get("effect") in sp.WRITE_EFFECTS and isinstance(node.get("id"), str):
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            node["idempotency_key"] = keys.idempotency_key(tenant_id, task_id, node["id"], inputs)
        elif node.get("kind") == "atomic":
            node.pop("idempotency_key", None)
    return out


def drop_unknown_tiers(stage: Any, task_plan: Any, tiers: tuple[str, ...]) -> tuple[Any, Any, list[str]]:
    """去掉不在 tiers 里的 tier，返回（Stage、Task Plan、去掉了什么的说明）；不是 dict 的原样返回。"""
    notes: list[str] = []
    allowed = set(tiers)
    if isinstance(stage, dict):
        stage = copy.deepcopy(stage)
        for node in nodes(stage):
            if "tier" in node and node["tier"] not in allowed:
                tier = node.pop("tier")
                notes.append(f"{node.get('id')} 的 Tier {tier!r} 不可用，改由 Model Gateway 按 role 选默认 Tier")
    if isinstance(task_plan, dict) and isinstance(task_plan.get("tiers"), dict):
        task_plan = copy.deepcopy(task_plan)
        kept = {}
        for name, tier in task_plan["tiers"].items():
            if tier in allowed:
                kept[name] = tier
            else:
                notes.append(f"Task Plan 的 tiers.{name} {tier!r} 不可用，已去掉")
        if kept:
            task_plan["tiers"] = kept
        else:
            del task_plan["tiers"]
    return stage, task_plan, notes


def task_budget_remaining(task_state: Any) -> float | None:
    """Task 还剩多少预算（美元）：constraints.budget_usd 减去各 Round 的 cost_usd，与 Reviewer 的台账同一口径；没有预算时为 None。"""
    if not isinstance(task_state, dict):
        return None
    budget = (task_state.get("constraints") or {}).get("budget_usd")
    if not isinstance(budget, int | float) or isinstance(budget, bool) or budget <= 0:
        return None
    spent = sum(float(r.get("cost_usd") or 0.0) for r in task_state.get("rounds") or [] if isinstance(r, dict))
    return round(float(budget) - spent, 6)


def cap_budgets(stage: Any, remaining: float | None) -> tuple[Any, list[str]]:
    """Stage 与各 Node 的预算超过 Task 剩余的，压到剩余（2026-10-28 线上日志：超出的 Node 被 budget_check 整个拒绝，白花一轮）。
    返回（Stage、压了什么的说明）；没有预算或已经用完时原样返回，用完由 budget_check 拒绝。"""
    if remaining is None or remaining <= 0 or not isinstance(stage, dict):
        return stage, []
    stage = copy.deepcopy(stage)
    notes: list[str] = []

    def cap(holder: Any, key: str, what: str) -> None:
        value = holder.get(key) if isinstance(holder, dict) else None
        if isinstance(value, int | float) and not isinstance(value, bool) and value > remaining:
            holder[key] = remaining
            notes.append(f"{what} 的预算 {value:g} 美元超过 Task 剩余的 {remaining:g} 美元，已压到剩余")

    cap(stage.get("budget"), "usd", "Stage")
    for node in nodes(stage):
        cap(node.get("budget"), "usd" if node.get("kind") == "goal" else "max_cost_usd", f"Node {node.get('id')}")
    return stage, notes


def _sub_agents(stage: Any) -> list[dict[str, Any]]:
    return [n for n in nodes(stage) if n.get("kind") == "goal" and n.get("mode") == "sub_agent"]


def _usd_of(holder: Any) -> float | None:
    value = holder.get("usd") if isinstance(holder, dict) else None
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0 else None


def share_sub_agent_budgets(stage: Any, remaining: float | None) -> tuple[Any, list[str]]:
    """N16-R（docs/plan/sub-agent.md 第 6 节）：每个 sub_agent 的份额 = min(brief.budget.usd, Node 自己的 budget.usd,
    Task 剩余 ÷ sub_agent 个数)，写进 Node 的 budget.usd（运行时按它给子工作流的美元预算）。都没有时不写。返回（Stage、说明）。"""
    agents = _sub_agents(stage)
    if not agents:
        return stage, []
    stage = copy.deepcopy(stage)
    share = round(remaining / len(agents), 6) if remaining is not None and remaining > 0 else None
    notes: list[str] = []
    for node in _sub_agents(stage):
        brief = (node.get("inputs") or {}).get("brief")
        own = _usd_of(node.get("budget"))
        options = [v for v in (_usd_of(brief.get("budget")) if isinstance(brief, dict) else None, own, share) if v is not None]
        if not options:
            continue
        usd = round(min(options), 6)
        if usd != own:
            node["budget"] = {**(node.get("budget") or {}), "usd": usd}
            notes.append(f"sub_agent {node.get('id')} 的份额定为 {usd:g} 美元")
    return stage, notes


# brief 的必填字段（docs/plan/sub-agent.md 第 4 节）。
BRIEF_FIELDS = ("objective", "scope", "output")

# 来源卡片（docs/plan/sub-agent.md 第 5 节）：子 Agent 交回结论与依据，不交回全文（全文在 ObjectStore，卡片里留 obj:// 路径）。
SOURCE_CARDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["cards"],
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["claim", "evidence", "source", "confidence"],
                "properties": {
                    "claim": {"type": "string", "minLength": 1},
                    "evidence": {"type": "string"},
                    "source": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "open_questions": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}
_GENERIC_OUTPUT = ({"type": "object"},)


def _wants_cards(brief: Any) -> bool:
    output = brief.get("output") if isinstance(brief, dict) else None
    return isinstance(output, str) and ("卡片" in output or "card" in output.lower())


def card_outputs(stage: Any) -> tuple[Any, list[str]]:
    """N16-R：brief 要来源卡片、又没给具体 output_schema（没写或只写了 {"type": "object"}）的 sub_agent，填上卡片的 schema。
    结构化输出在 tool_loop 的 activity 里按它校验。返回（Stage、说明）。"""
    agents = [n for n in _sub_agents(stage) if _wants_cards((n.get("inputs") or {}).get("brief")) and n.get("output_schema") in (None, *_GENERIC_OUTPUT)]
    if not agents:
        return stage, []
    stage = copy.deepcopy(stage)
    notes = []
    for node in _sub_agents(stage):
        if _wants_cards((node.get("inputs") or {}).get("brief")) and node.get("output_schema") in (None, *_GENERIC_OUTPUT):
            node["output_schema"] = copy.deepcopy(SOURCE_CARDS_SCHEMA)
            notes.append(f"sub_agent {node.get('id')} 按 brief 交回来源卡片")
    return stage, notes


def _sub_agent_problems(stage: dict[str, Any], level: str) -> list[str]:
    """N16-R：派子 Agent 的规矩（AR_SUB_AGENT_POLICY=rules）。"""
    agents = _sub_agents(stage)
    if not agents:
        return []
    if level in ("fast", "light"):
        return [f"Triage 是 {level} 时不派 sub_agent（{'、'.join(str(a.get('id')) for a in agents)}）：交给主流程的 tool_loop"]
    found: list[str] = []
    for node in agents:
        brief = (node.get("inputs") or {}).get("brief")
        if not isinstance(brief, dict):
            found.append(f"sub_agent {node.get('id')} 缺 inputs.brief：写清 objective（要回答什么）、scope（查什么、到哪为止、不做什么）、output（交回格式）")
            continue
        missing = [f for f in BRIEF_FIELDS if not (isinstance(brief.get(f), str) and brief[f].strip()) and not (f == "output" and isinstance(brief.get(f), dict) and brief[f])]
        if missing:
            found.append(f"sub_agent {node.get('id')} 的 brief 缺 {'、'.join(missing)}")
    if len(agents) >= 2:
        ids = {str(a.get("id")) for a in agents}
        if not any(ids <= {str(x) for x in n.get("after") or []} for n in nodes(stage)):
            found.append(
                f"有 {len(agents)} 个 sub_agent（{'、'.join(sorted(ids))}），后面要接一个综合节点：after 列出它们全部，"
                "输入引用各自的结果，列出互相矛盾的卡片与没查到的问题"
            )
    return found


def _rank(effect: str) -> int:
    return sp.effect_rank(effect) if effect in sp.EFFECT_ORDER else len(sp.EFFECT_ORDER)


def fallback_tools(context: PlanningContext) -> list[str]:
    """保守规划的 Goal Node 能用的工具：effect 上限以内的都给，否则它读不了也改不了（TMP 计划第 11.15 节）。不可逆的不给：那要逐次审批，
    不该由回退路径替模型选上。排好序，结果确定。"""
    return sorted(
        name for name, effect in context.catalog.tools.items() if effect != "write_irreversible" and _rank(effect) <= _rank(context.effect_ceiling)
    )


def _catalog_problems(stage: dict[str, Any], context: PlanningContext) -> list[str]:
    catalog = context.catalog
    found: list[str] = []
    for node in nodes(stage):
        nid = node.get("id")
        refs: list[tuple[str, str, dict[str, Any]]] = []
        if node.get("kind") == "atomic" and node.get("template"):
            refs.append(("Node Template", str(node["template"]), catalog.templates))
        if node.get("kind") == "goal":
            if node.get("recipe"):
                refs.append(("Recipe", str(node["recipe"]), catalog.recipes))
            refs += [("工具", str(t), catalog.tools) for t in node.get("tools") or []]
        quality = node.get("quality") if isinstance(node.get("quality"), dict) else {}
        refs += [("Node Template", str(quality[k]), catalog.templates) for k in ("judge", "scorer") if quality.get(k)]
        found += [f"{nid} 用的{kind} {ref} 不在 Node Catalog 里，只能用 Catalog 列出的" for kind, ref, known in refs if ref not in known]
    return found


def _effect_problems(stage: dict[str, Any], context: PlanningContext) -> list[str]:
    ceiling = context.effect_ceiling
    found: list[str] = []
    for node in nodes(stage):
        nid = node.get("id")
        declared = sp.node_effect(node, ceiling)
        if _rank(declared) > _rank(ceiling):
            found.append(f"{nid} 的 effect {declared} 超过上限 {ceiling}")
        if node.get("kind") == "atomic":
            template = context.catalog.templates.get(str(node.get("template")))
            if template and _rank(str(node.get("effect"))) < _rank(template[1]):
                found.append(f"{nid} 按 {node.get('effect')} 声明，低于模板 {node.get('template')} 的 {template[1]}：effect 要按模板写")
        else:
            over = sorted(str(t) for t in node.get("tools") or [] if t in context.catalog.tools and _rank(context.catalog.tools[t]) > _rank(declared))
            if over:
                found.append(f"{nid} 的工具 {'、'.join(over)} 高于它自己的 effect 上限 {declared}：调高 effect_ceiling 或去掉这些工具")
    return found


def _role_problems(stage: dict[str, Any], context: PlanningContext) -> list[str]:
    found: list[str] = []
    for node in nodes(stage):
        if node.get("kind") != "atomic" or node.get("role") not in TOOL_ROLES:
            continue
        template = context.catalog.templates.get(str(node.get("template")))
        if node.get("executor") == "model" or (template is not None and template[0] == "model"):
            found.append(
                f"{node.get('id')} 担任 {node.get('role')}，却由 model 执行（模板 {node.get('template')}）：model 节点没有工具，读不到文件、跑不了命令、"
                "也做不了写操作，只会编出结果。要读写文件或执行命令，放进一个带 tools 的 tool_loop Goal Node，或用 Catalog 里 executor 为 program 的模板"
            )
    return found


def problems(
    stage: Any,
    task_plan: Any,
    *,
    level: str,
    context: PlanningContext,
    keep_nodes: list[str],
    earlier: Mapping[str, str] | None = None,
    require_steps: bool = False,
    sub_agent_rules: bool = False,
) -> list[str]:
    """不合格之处的说明，为空表示合格。说明会原样带给模型重问。earlier 给了时按 N-01 的规则查引用（与 Stage Runner 一致）。
    Task Plan 带了 steps 时按 N-04 查（workflows/plan.py，与 Task 工作流一致）；require_steps（第一次完整规划）时没给也不合格。
    sub_agent_rules（N16-R，AR_SUB_AGENT_POLICY=rules）时另查派子 Agent 的规矩（_sub_agent_problems）。"""
    if not isinstance(stage, dict):
        return ["没有给出 stage（JSON 对象）"]
    found = schema_problems("stage", stage)
    if not found:
        found += [
            f"Stage 的 DAG：{p}"
            for p in sp.validate_stage(stage, max_nodes=MAX_NODES, earlier=None if earlier is None else set(earlier), max_sub_agents=sp.MAX_SUB_AGENTS)
        ]
    found += _catalog_problems(stage, context)
    found += _effect_problems(stage, context)
    found += _role_problems(stage, context)
    if sub_agent_rules:
        found += _sub_agent_problems(stage, level)
    redone = sorted({str(n.get("id")) for n in nodes(stage)} & set(keep_nodes))
    if redone:
        found.append(f"重做了上一轮已验证、要保留的 Node：{'、'.join(redone)}；直接用它们的结果，不要再安排")
    if level in ("full", "light", "replan"):
        if not isinstance(task_plan, dict):
            found.append("没有给出 task_plan（JSON 对象）")
        else:
            found += schema_problems("task_plan", task_plan)
            steps = task_plan.get("steps")
            if steps is not None:
                seq = stage.get("seq")
                found += [p for p in plan_steps.step_problems(steps, seq if isinstance(seq, int) else 1) if p not in found]
            elif require_steps:
                found.append("第一次完整规划要在 task_plan.steps 给出 3～8 步的整体计划（见规则 10）")
    return found
