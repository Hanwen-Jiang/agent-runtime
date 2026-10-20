"""评测用的 Stage 工具：按运行时的做法补幂等键、校验 DAG 与 Catalog 引用、算 Node 的 effect。

与运行时保持一致的地方都直接复用 Worker 的代码（stage_plan、keys），不另写一套。
"""

from __future__ import annotations

import copy
from typing import Any

from agentruntime_worker.executor import keys
from agentruntime_worker.workflows import stage_plan as sp

from agentruntime_learning.evals.model import Catalog

EFFECT_ORDER = sp.EFFECT_ORDER
WRITE_EFFECTS = sp.WRITE_EFFECTS
# 评测里生成幂等键用的租户与 Task id：幂等键由代码生成后填入，不由 Planner 编写（附录 D.4），评测照做。
EVAL_TENANT = "evals"
EVAL_TASK_ID = "tsk_01J" + "0" * 22 + "E"
# 顶层 Node 数的上限，与 StagePolicy 的默认值相同。
MAX_NODES = 200


def rank(effect: str) -> int:
    return EFFECT_ORDER.index(effect) if effect in EFFECT_ORDER else len(EFFECT_ORDER)


def nodes(stage: Any) -> list[dict[str, Any]]:
    if not isinstance(stage, dict) or not isinstance(stage.get("nodes"), list):
        return []
    return [n for n in stage["nodes"] if isinstance(n, dict)]


def with_idempotency_keys(stage: Any) -> Any:
    """写操作的 Atomic Node 填上代码生成的幂等键（覆盖 Planner 写的），与 Stage Runner 的做法相同。"""
    if not isinstance(stage, dict):
        return stage
    filled = copy.deepcopy(stage)
    for node in nodes(filled):
        if node.get("kind") == "atomic" and node.get("effect") in WRITE_EFFECTS and isinstance(node.get("id"), str):
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            node["idempotency_key"] = keys.idempotency_key(EVAL_TENANT, EVAL_TASK_ID, node["id"], inputs)
    return filled


def dag_problems(stage: dict[str, Any]) -> list[str]:
    """JSON Schema 表达不了的 Stage 规则（Node id 唯一、after 存在、无环、输入引用上游），复用 Stage Runner 的检查。"""
    return sp.validate_stage(stage, max_nodes=MAX_NODES)


def catalog_problems(stage: dict[str, Any], catalog: Catalog) -> list[str]:
    """引用了 Catalog 里没有的 Node Template（含 quality 的 judge 与 scorer）、Recipe 或工具。"""
    found: list[str] = []
    for node in nodes(stage):
        refs: list[tuple[str, str]] = []
        if node.get("kind") == "atomic" and node.get("template"):
            refs.append(("Node Template", str(node["template"])))
        if node.get("kind") == "goal":
            if node.get("recipe"):
                refs.append(("Recipe", str(node["recipe"])))
            refs += [("工具", str(t)) for t in node.get("tools") or []]
        quality = node.get("quality") if isinstance(node.get("quality"), dict) else {}
        refs += [("Node Template", str(quality[k])) for k in ("judge", "scorer") if quality.get(k)]
        for kind, ref in refs:
            known = {"Node Template": catalog.templates, "Recipe": catalog.recipes, "工具": catalog.tools}[kind]
            if ref not in known:
                found.append(f"{node.get('id')} 引用的{kind} {ref} 不在 Catalog 里")
    return found


def tool_effect(catalog: Catalog, tool: str) -> str:
    """查不到的工具按最高的 effect 处理，与 Stage Runner 相同。"""
    return catalog.tools.get(tool, "write_irreversible")


def declared_effect(node: dict[str, Any], ceiling: str) -> str:
    """Node 声明的 effect：Atomic 取 effect；Goal 取 effect_ceiling，sub_agent 缺省 read，其余缺省为 Task 的上限。"""
    return sp.node_effect(node, ceiling)


def write_effect(node: dict[str, Any], catalog: Catalog) -> str:
    """Node 实际可能产生的写入：Atomic 取声明的 effect；tool_loop 取所列工具的最高 effect（运行时按它持写锁）；
    sub_agent 只读；template、sub_task 取 effect_ceiling（没写时按 read）。"""
    if node.get("kind") != "goal":
        return str(node.get("effect", "none"))
    mode = node.get("mode") or "tool_loop"
    if mode == "sub_agent":
        return "read"
    if mode == "tool_loop":
        return max((tool_effect(catalog, str(t)) for t in node.get("tools") or []), key=rank, default="none")
    return str(node.get("effect_ceiling") or "read")


def ancestors(stage: dict[str, Any]) -> dict[str, set[str]]:
    """每个 Node 的全部上游（after 的传递闭包）；DAG 不合法时尽量算。"""
    deps = {str(n.get("id")): [str(d) for d in n.get("after") or []] for n in nodes(stage)}
    found: dict[str, set[str]] = {}

    def visit(nid: str, seen: frozenset[str]) -> set[str]:
        if nid in found:
            return found[nid]
        result: set[str] = set()
        for dep in deps.get(nid, []):
            if dep in deps and dep not in seen:
                result |= {dep} | visit(dep, seen | {dep})
        found[nid] = result
        return result

    for nid in sorted(deps):
        visit(nid, frozenset({nid}))
    return found


def levels(stage: dict[str, Any]) -> dict[str, int]:
    """Node 的层次：没有上游的是 0，否则是最深的上游加 1。"""
    deps = {str(n.get("id")): [str(d) for d in n.get("after") or [] if str(d) != str(n.get("id"))] for n in nodes(stage)}
    found: dict[str, int] = {}

    def depth(nid: str, seen: frozenset[str]) -> int:
        if nid in found:
            return found[nid]
        parents = [d for d in deps.get(nid, []) if d in deps and d not in seen]
        value = 0 if not parents else 1 + max(depth(d, seen | {d}) for d in parents)
        found[nid] = value
        return value

    for nid in sorted(deps):
        depth(nid, frozenset({nid}))
    return found
