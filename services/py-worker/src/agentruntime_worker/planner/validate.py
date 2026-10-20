"""Planner 输出的校验与规整（计划 S-09 难点“结构化输出”“不能编造模板”）。

规整（normalize）由代码完成：Stage 的 seq 定为本轮序号；写操作的幂等键按 tenant_id、task_id、node_id 与输入重新生成
（附录 D.4，模型写的不算数）。校验（problems）不过就带着问题重问模型：

- Stage 与 Task Plan 合 /schemas；Stage 的 DAG 合法（复用 Stage Runner 的检查）。
- Node Template、Recipe、工具（含 quality 的 judge 与 scorer）都在 Node Catalog 里。
- effect 不超过上限；Atomic Node 的 effect 不低于模板的 effect；Goal Node 所列工具不高于它自己的 effect 上限。
- replan：Reviewer 保留的 Node 不得在新 Stage 里重做。

这里的检查与 S-08 评分器各写各的，评分器才算独立的核对。
"""

from __future__ import annotations

import copy
import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema_specifications import REGISTRY as SPECIFICATIONS
from referencing import Registry, Resource

from agentruntime_worker.executor import keys
from agentruntime_worker.planner.context import PlanningContext
from agentruntime_worker.workflows import stage_plan as sp

# /schemas 随仓库发布；Worker 镜像里放在同样的相对位置。
SCHEMAS_DIR = Path(__file__).resolve().parents[5] / "schemas"
# 顶层 Node 数的上限，与 StagePolicy 的默认值相同。
MAX_NODES = 200


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


def _rank(effect: str) -> int:
    return sp.effect_rank(effect) if effect in sp.EFFECT_ORDER else len(sp.EFFECT_ORDER)


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


def problems(
    stage: Any,
    task_plan: Any,
    *,
    level: str,
    context: PlanningContext,
    keep_nodes: list[str],
) -> list[str]:
    """不合格之处的说明，为空表示合格。说明会原样带给模型重问。"""
    if not isinstance(stage, dict):
        return ["没有给出 stage（JSON 对象）"]
    found = schema_problems("stage", stage)
    if not found:
        found += [f"Stage 的 DAG：{p}" for p in sp.validate_stage(stage, max_nodes=MAX_NODES)]
    found += _catalog_problems(stage, context)
    found += _effect_problems(stage, context)
    redone = sorted({str(n.get("id")) for n in nodes(stage)} & set(keep_nodes))
    if redone:
        found.append(f"重做了上一轮已验证、要保留的 Node：{'、'.join(redone)}；直接用它们的结果，不要再安排")
    if level in ("full", "light", "replan"):
        if not isinstance(task_plan, dict):
            found.append("没有给出 task_plan（JSON 对象）")
        else:
            found += schema_problems("task_plan", task_plan)
    return found
