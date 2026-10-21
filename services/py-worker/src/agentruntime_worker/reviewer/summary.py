"""汇总（设计书 3.2.5 核心逻辑第 1 条）：把 Stage 结果写进 Task State——已验证的事实、待查的假设、产物。

state_update 由 Reviewer 给出（模型提议、代码检查与截断；产物由代码从结果里取），工作流用 apply_state_update
写进 Task State。纯函数，不做 I/O。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

STATE_UPDATE_KEYS = ("facts", "assumptions_verified", "assumptions_broken", "open_questions", "artifacts")
_OBJECT_URI = re.compile(r"^obj://([a-z0-9][a-z0-9_-]{0,62})/\S+$")


def _texts(value: Any, limit: int) -> list[str]:
    items = value if isinstance(value, list) else []
    out = list(dict.fromkeys(s.strip() for s in items if isinstance(s, str) and s.strip()))
    return out[:limit]


def checked_update(raw: Any, *, max_items: int) -> dict[str, list[str]]:
    """模型提议的汇总：只留认识的键与非空字符串，去重，每项最多 max_items 条。产物不收模型给的（由代码从结果里取）。"""
    raw = raw if isinstance(raw, Mapping) else {}
    out: dict[str, list[str]] = {}
    for key in STATE_UPDATE_KEYS[:-1]:
        items = _texts(raw.get(key), max_items)
        if items:
            out[key] = items
    return out


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def artifacts_in(results: Mapping[str, Mapping[str, Any]], tenant_id: str) -> list[str]:
    """成功的 Node 结果里出现的、本租户前缀的 ObjectStore 对象，按字典序。其他租户的对象一律不收。"""
    found = {
        s
        for r in results.values()
        if r.get("status") == "succeeded"
        for s in _strings(r.get("result"))
        if (m := _OBJECT_URI.match(s)) and m.group(1) == tenant_id
    }
    return sorted(found)


def _append(target: list[Any], items: Iterable[Any]) -> None:
    for item in items:
        if item not in target:
            target.append(item)


def apply_state_update(task_state: dict[str, Any], update: Mapping[str, Any]) -> None:
    """事实去重追加；验证过的假设转成事实，被证伪的从假设里拿掉并记成事实；待查的问题与产物去重追加。"""
    facts = task_state.setdefault("facts", [])
    assumptions = task_state.setdefault("assumptions", [])
    verified = [a for a in update.get("assumptions_verified") or [] if isinstance(a, str) and a]
    broken = [a for a in update.get("assumptions_broken") or [] if isinstance(a, str) and a]
    _append(facts, (f for f in update.get("facts") or [] if isinstance(f, str) and f))
    _append(facts, verified)
    _append(facts, (f"假设不成立：{a}" for a in broken))
    task_state["assumptions"] = [a for a in assumptions if a not in verified and a not in broken]
    _append(task_state.setdefault("open_questions", []), (q for q in update.get("open_questions") or [] if isinstance(q, str) and q))
    _append(task_state.setdefault("artifacts", []), (a for a in update.get("artifacts") or [] if isinstance(a, str) and _OBJECT_URI.match(a)))
