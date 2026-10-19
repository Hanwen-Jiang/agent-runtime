"""Stage Runner 的纯函数（设计书 3.2.3、附录 D、E、F）：校验 Stage、解析输入引用、给展开的子图编号、
应用 Hook 的改写、默认 Hook、投票与剪枝、批量切分、确定的 id。

这些函数不做 I/O、不读时间、不用随机数，工作流代码直接调用；同样的输入永远得到同样的输出，重放才对得上。

展开出的 Node id 一律是“父 id + 序号”（计划 S-04 难点“确定的 Node id”）：
  - quality revise：第 r 轮的生成是 <id>.<2r-1>，judge 是 <id>.<2r>。
  - quality ensemble：候选 <id>.1 … <id>.n；aggregate = judge 时，judge 是 <id>.<n+1>。
  - quality search：按生成顺序编号；每层先是这一层的候选，再是这一层的 scorer。
  - Goal template：固定子图按 Recipe 的顺序编号 <id>.1 …；逐条目展开时第 i 个条目链上的第 j 个 Node 是 <id>.<i>.<j>。
  - Goal tool_loop（含 sub_agent 内部）：第 i 步的思考是 <id>.<2i-1>，工具调用是 <id>.<2i>。
  - write_irreversible 的 Node 前自动插入的人工审批 gate 是 <id>.1（这类 Node 不允许挂 quality，没有别的子节点）。
Planner 写的顶层 Node id 不含“.”，与展开出的 id 不会冲突。
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

EFFECT_ORDER = ("none", "read", "write_reversible", "write_irreversible")
WRITE_EFFECTS = ("write_reversible", "write_irreversible")
APPROVAL_OPTIONS = ("approve", "reject")

# 同一个错误连续出现几次就打断循环（附录 B.8）。
REPEATED_ERROR_LIMIT = 3
# Goal Node 嵌套深度上限（附录 E.1）。
MAX_SUB_TASK_DEPTH = 2

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# $<node_id>.result[.路径]：node_id 本身可以带“.序号”，取第一个“.result”作分界。
_NODE_REF = re.compile(r"^([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*?)\.result(?:\.(.+))?$")
_SPECIAL_REFS = ("task", "goal", "item")


class UnresolvedReference(ValueError):
    """输入里的 $ 引用找不到，或引用了 result 以外的东西。"""


class PatchError(ValueError):
    """Hook 给出的 JSON Patch 无法应用。"""


def effect_rank(effect: str) -> int:
    """effect 的高低：none < read < write_reversible < write_irreversible；未知的 effect 抛 ValueError。"""
    if effect not in EFFECT_ORDER:
        raise ValueError(f"未知的 effect {effect!r}")
    return EFFECT_ORDER.index(effect)


def node_effect(node: Mapping[str, Any], task_ceiling: str) -> str:
    """Node 的 effect：Atomic Node 取 effect；Goal Node 取 effect_ceiling，sub_agent 缺省为 read，其余缺省为 task_ceiling。"""
    if node.get("kind") == "goal":
        if node.get("effect_ceiling"):
            return str(node["effect_ceiling"])
        return "read" if node.get("mode") == "sub_agent" else task_ceiling
    return str(node.get("effect", "none"))


def quality_type(node: Mapping[str, Any]) -> str:
    return str((node.get("quality") or {}).get("type", "none"))


def _references(value: Any) -> Iterator[str]:
    """输入里所有的 $ 引用（去掉开头的 $），不含 $$ 转义的字面量。"""
    if isinstance(value, Mapping):
        for v in value.values():
            yield from _references(v)
    elif isinstance(value, list):
        for v in value:
            yield from _references(v)
    elif isinstance(value, str) and value.startswith("$") and not value.startswith("$$"):
        yield value[1:]


def _node_reference(body: str) -> tuple[str, str] | None:
    """解析 <node_id>.result[.路径]；不是 Node 的 result 引用时返回 None。"""
    match = _NODE_REF.match(body)
    if not match:
        return None
    return match.group(1), match.group(2) or ""


def validate_stage(stage: Mapping[str, Any], *, max_nodes: int, top_level: bool = True) -> list[str]:
    """校验 JSON Schema 表达不了的规则，返回错误说明（为空表示合法）：

    - Node id 唯一，顶层 id 不含“.”；after 引用存在；DAG 无环；
    - 输入里的 $<id>.result 只能引用祖先 Node（after 的传递闭包）；
    - 写操作的 Atomic Node 与 write_irreversible 的 Node 不能挂 quality（生成会重做）；
    - 顶层 Node 个数不超过 max_nodes。

    top_level 为 False 时用于校验展开出的子图，Node id 可以带“.”。
    """
    errors: list[str] = []
    nodes = list(stage.get("nodes", []))
    if len(nodes) > max_nodes:
        errors.append(f"Stage 有 {len(nodes)} 个 Node，超过上限 {max_nodes}")
    ids: list[str] = [str(n.get("id", "")) for n in nodes]
    seen: list[str] = []
    for nid in ids:
        if nid in seen:
            errors.append(f"Node id 重复：{nid}")
        seen.append(nid)
        if top_level and "." in nid:
            errors.append(f"顶层 Node id 不能含“.”（留给展开出的子 Node）：{nid}")
    deps = {str(n.get("id", "")): [str(d) for d in n.get("after", [])] for n in nodes}
    for nid in sorted(deps):
        for dep in deps[nid]:
            if dep not in deps:
                errors.append(f"{nid} 的 after 引用了不存在的 Node：{dep}")
    # Kahn 拓扑排序，剩下的就是环。
    indegree = {nid: sum(1 for d in deps[nid] if d in deps) for nid in deps}
    ready = sorted(nid for nid, n in indegree.items() if n == 0)
    ordered: list[str] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for nid in sorted(deps):
            if current in deps[nid]:
                indegree[nid] -= 1
                if indegree[nid] == 0:
                    ready.append(nid)
        ready.sort()
    if len(ordered) < len(deps):
        errors.append(f"Node 的依赖有环：{sorted(set(deps) - set(ordered))}")
        return errors
    ancestors: dict[str, list[str]] = {}
    for nid in ordered:
        found: list[str] = []
        for dep in deps[nid]:
            for a in [dep, *ancestors.get(dep, [])]:
                if a not in found:
                    found.append(a)
        ancestors[nid] = found
    for node in nodes:
        nid = str(node.get("id", ""))
        for body in _references(node.get("inputs", {})):
            head = body.split(".", 1)[0]
            if head in _SPECIAL_REFS:
                continue
            parsed = _node_reference(body)
            if parsed is None:
                errors.append(f"{nid} 的输入 ${body} 只能引用上游的 result、Task State 或常量")
            elif parsed[0] not in ancestors.get(nid, []):
                errors.append(f"{nid} 的输入引用了 {parsed[0]}，它不是 {nid} 的上游")
        # 写操作的 Atomic Node 重做就是再写一次，不能挂 quality；write_reversible 的 Goal Node 可以（附录 B.3
        # “修复 CI”：tool_loop 是唯一写者，revise 带着测试结果再试）；write_irreversible 一律不行。
        effect = node_effect(node, "read")
        if quality_type(node) != "none" and (
            effect == "write_irreversible" or (node.get("kind") != "goal" and effect in WRITE_EFFECTS)
        ):
            errors.append(f"{nid}：写操作不能挂 quality（生成会重做）")
    return errors


_MISSING = object()


def _walk(value: Any, path: str, ref: str) -> Any:
    current = value
    if path:
        for part in path.split("."):
            if isinstance(current, Mapping) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                raise UnresolvedReference(f"{ref}：找不到 {part}")
    return copy.deepcopy(current)


def resolve_inputs(
    value: Any,
    *,
    results: Mapping[str, Any],
    task_state: Mapping[str, Any],
    goal_inputs: Mapping[str, Any] | None = None,
    item: Any = _MISSING,
) -> Any:
    """把输入里的引用换成值（附录 D.1：只能引用上游 result、Task State 或常量）。

    整个字符串是一个引用：$<node_id>.result[.路径]、$task.<路径>、$goal.<路径>（Recipe 子图引用 Goal 的输入）、
    $item[.路径]（逐条目展开时的当前条目）。$$ 开头表示字面的 $。dict 与 list 递归解析，其余原样返回。
    """
    if isinstance(value, Mapping):
        return {k: resolve_inputs(v, results=results, task_state=task_state, goal_inputs=goal_inputs, item=item) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_inputs(v, results=results, task_state=task_state, goal_inputs=goal_inputs, item=item) for v in value]
    if not isinstance(value, str) or not value.startswith("$"):
        return value
    if value.startswith("$$"):
        return value[1:]
    body = value[1:]
    head, _, rest = body.partition(".")
    if head == "task":
        return _walk(task_state, rest, value)
    if head == "goal":
        if goal_inputs is None:
            raise UnresolvedReference(f"{value}：这里没有 Goal 的输入")
        return _walk(goal_inputs, rest, value)
    if head == "item":
        if item is _MISSING:
            raise UnresolvedReference(f"{value}：这里没有当前条目")
        return _walk(item, rest, value)
    parsed = _node_reference(body)
    if parsed is None:
        raise UnresolvedReference(f"{value}：只能引用上游 result、Task State 或常量")
    node_id, path = parsed
    if node_id not in results:
        raise UnresolvedReference(f"{value}：上游 {node_id} 没有结果")
    return _walk(results[node_id], path, value)


def child_id(parent: str, *seq: int) -> str:
    """展开出的子 Node id：父 id 加序号，例如 child_id("analyze", 3) == "analyze.3"。"""
    return ".".join([parent, *(str(s) for s in seq)])


def _rewrite(value: Any, mapping: Mapping[str, str], *, item: Any = _MISSING) -> Any:
    """改写局部 id 的 result 引用；给了 item 时把 $item 引用换成条目的值（值里的 $ 转义成 $$）。"""
    if isinstance(value, Mapping):
        return {k: _rewrite(v, mapping, item=item) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite(v, mapping, item=item) for v in value]
    if not isinstance(value, str) or not value.startswith("$") or value.startswith("$$"):
        return value
    body = value[1:]
    head, _, rest = body.partition(".")
    if head == "item" and item is not _MISSING:
        return _escape(_walk(item, rest, value))
    if head in _SPECIAL_REFS:
        return value
    parsed = _node_reference(body)
    if parsed is None or parsed[0] not in mapping:
        raise ValueError(f"子图里的引用 {value} 指向不存在的 Node")
    node_id, path = parsed
    return f"${mapping[node_id]}.result" + (f".{path}" if path else "")


def _escape(value: Any) -> Any:
    """把常量里以 $ 开头的字符串转义成 $$，免得之后被当成引用解析。"""
    if isinstance(value, Mapping):
        return {k: _escape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_escape(v) for v in value]
    if isinstance(value, str) and value.startswith("$"):
        return "$" + value
    return value


def _renumber(nodes: list[dict[str, Any]], mapping: Mapping[str, str], *, item: Any = _MISSING) -> list[dict[str, Any]]:
    out = []
    for node in nodes:
        new = copy.deepcopy(node)
        new["id"] = mapping[node["id"]]
        if "after" in new:
            missing = [d for d in new["after"] if d not in mapping]
            if missing:
                raise ValueError(f"子图里 {node['id']} 的 after 引用了不存在的 Node：{missing}")
            new["after"] = [mapping[d] for d in new["after"]]
        if "inputs" in new:
            new["inputs"] = _rewrite(new["inputs"], mapping, item=item)
        out.append(new)
    return out


def reindex_subgraph(goal_id: str, nodes: list[dict[str, Any]], output: str) -> tuple[list[dict[str, Any]], str]:
    """给 Recipe 的固定子图编号：局部 id 按出现顺序换成 <goal_id>.<n>，after 与 $<局部 id>.result 引用随之改写。
    返回新的 Node 列表与 output 的新 id。局部 id 重复、引用不存在时抛 ValueError。"""
    mapping: dict[str, str] = {}
    for i, node in enumerate(nodes, start=1):
        if node["id"] in mapping:
            raise ValueError(f"子图里的 Node id 重复：{node['id']}")
        mapping[node["id"]] = child_id(goal_id, i)
    if output not in mapping:
        raise ValueError(f"子图里没有 output 指定的 Node：{output}")
    return _renumber(nodes, mapping), mapping[output]


def expand_per_item(goal_id: str, chain: list[dict[str, Any]], items: list[Any], collect: str) -> tuple[list[list[dict[str, Any]]], list[str]]:
    """逐条目展开：第 i 个条目（从 1 开始）的链编号为 <goal_id>.<i>.<j>，$item 引用换成该条目，链内引用改写。
    返回每个条目的 Node 列表，以及每个条目给出结果的 Node id（collect 的新 id）。"""
    local = [n["id"] for n in chain]
    if len(set(local)) != len(local):
        raise ValueError("逐条目展开的链里 Node id 重复")
    if collect not in local:
        raise ValueError(f"链里没有 collect 指定的 Node：{collect}")
    chains: list[list[dict[str, Any]]] = []
    collected: list[str] = []
    for i, item in enumerate(items, start=1):
        mapping = {lid: child_id(goal_id, i, j) for j, lid in enumerate(local, start=1)}
        chains.append(_renumber(chain, mapping, item=item))
        collected.append(mapping[collect])
    return chains, collected


def batches(count: int, batch_size: int) -> list[range]:
    """把 count 个条目按 batch_size 切成连续的批次。"""
    if batch_size < 1:
        raise ValueError("batch_size 必须大于 0")
    return [range(start, min(start + batch_size, count)) for start in range(0, count, batch_size)]


def _pointer(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise PatchError(f"JSON Pointer 必须以 / 开头：{path!r}")
    return [t.replace("~1", "/").replace("~0", "~") for t in path[1:].split("/")]


def _parent(doc: Any, tokens: list[str]) -> Any:
    current = doc
    for token in tokens[:-1]:
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            raise PatchError(f"路径不存在：/{'/'.join(tokens)}")
    return current


def _get(doc: Any, tokens: list[str]) -> Any:
    if not tokens:
        return doc
    parent, key = _parent(doc, tokens), tokens[-1]
    if isinstance(parent, dict) and key in parent:
        return parent[key]
    if isinstance(parent, list) and key.isdigit() and int(key) < len(parent):
        return parent[int(key)]
    raise PatchError(f"路径不存在：/{'/'.join(tokens)}")


def _add(doc: Any, tokens: list[str], value: Any) -> Any:
    if not tokens:
        return value
    parent, key = _parent(doc, tokens), tokens[-1]
    if isinstance(parent, dict):
        parent[key] = value
    elif isinstance(parent, list):
        if key == "-":
            parent.append(value)
        elif key.isdigit() and int(key) <= len(parent):
            parent.insert(int(key), value)
        else:
            raise PatchError(f"数组下标越界：{key}")
    else:
        raise PatchError("只能往对象或数组里添加")
    return doc


def _remove(doc: Any, tokens: list[str]) -> Any:
    if not tokens:
        raise PatchError("不能删除整个文档")
    _get(doc, tokens)
    parent, key = _parent(doc, tokens), tokens[-1]
    if isinstance(parent, dict):
        del parent[key]
    else:
        del parent[int(key)]
    return doc


def apply_patch(doc: Any, patch: list[Mapping[str, Any]]) -> Any:
    """应用 RFC 6902 JSON Patch（add、remove、replace、move、copy、test），返回新文档，不改原文档。"""
    result = copy.deepcopy(doc)
    for op in patch:
        kind = op.get("op")
        path = _pointer(str(op.get("path", "")))
        if kind == "add":
            result = _add(result, path, copy.deepcopy(op.get("value")))
        elif kind == "remove":
            result = _remove(result, path)
        elif kind == "replace":
            _get(result, path)
            result = _add(_remove(result, path) if path else result, path, copy.deepcopy(op.get("value")))
        elif kind == "move":
            source = _pointer(str(op.get("from", "")))
            value = _get(result, source)
            result = _add(_remove(result, source), path, value)
        elif kind == "copy":
            source = _pointer(str(op.get("from", "")))
            result = _add(result, path, copy.deepcopy(_get(result, source)))
        elif kind == "test":
            if _get(result, path) != op.get("value"):
                raise PatchError(f"test 不通过：{op.get('path')}")
        else:
            raise PatchError(f"不认识的操作：{kind}")
    return result


def default_node_hooks(node: Mapping[str, Any], task_ceiling: str) -> dict[str, list[str]]:
    """按 effect 自动插入的 Hook（附录 F.3），返回 {"before_node": [...], "after_node": [...]}。

    所有 Node：budget_check（前）、audit_log（后，总在最后）；read：trace_result（后）；
    write_reversible：idempotency_check、single_writer_lock（前），checkpoint（后）；
    write_irreversible：idempotency_check（前）。人工审批不是 Hook 调用，而是插入的 gate 节点。
    """
    effect = node_effect(node, task_ceiling)
    before = ["budget_check"]
    after: list[str] = []
    if effect == "read":
        after.append("trace_result")
    elif effect == "write_reversible":
        before += ["idempotency_check", "single_writer_lock"]
        after.append("checkpoint")
    elif effect == "write_irreversible":
        before.append("idempotency_check")
    after.append("audit_log")
    return {"before_node": before, "after_node": after}


def vote(candidates: list[Any]) -> int:
    """ensemble 的投票：按规范化的 JSON 计票，票数最多者胜，平票取序号小的。返回胜者下标（从 0 开始）。"""
    if not candidates:
        raise ValueError("没有候选")
    keys = [json.dumps(c, sort_keys=True, ensure_ascii=False) for c in candidates]
    counts = [keys.count(k) for k in keys]
    return counts.index(max(counts))


def prune(scores: list[float], beam_width: int) -> list[int]:
    """search 的剪枝：按分数从高到低保留 beam_width 个，同分取下标小的。返回保留的下标，按名次排序。"""
    return sorted(range(len(scores)), key=lambda i: (-scores[i], i))[:beam_width]


def repeated_error(history: list[Mapping[str, Any]], limit: int = REPEATED_ERROR_LIMIT) -> bool:
    """tool_loop 最近 limit 步是否都以同一个错误结束（同一工具、同一错误说明）。"""
    recent = history[-limit:]
    if len(recent) < limit or any("error" not in step for step in recent):
        return False
    signatures = {(step.get("tool"), step.get("error")) for step in recent}
    return len(signatures) == 1


def ulid_task_id(ms: int, random_bits: int) -> str:
    """由毫秒时间戳与 80 位随机数构造 task_id（tsk_ 加 26 位 ULID）。工作流里用 workflow.now() 与 workflow.random()。"""
    value = ((ms & (2**48 - 1)) << 80) | (random_bits & (2**80 - 1))
    chars = [_CROCKFORD[(value >> (5 * (25 - i))) & 31] for i in range(26)]
    return "tsk_" + "".join(chars)


def subworkflow_id(task_id: str, round_seq: int, node_id: str, suffix: str = "") -> str:
    """Goal Node 子工作流的 ID：<task_id>/r<round_seq>/<node_id>[/<suffix>]。同一 Task 内唯一，重放时不变。"""
    base = f"{task_id}/r{round_seq}/{node_id}"
    return f"{base}/{suffix}" if suffix else base
