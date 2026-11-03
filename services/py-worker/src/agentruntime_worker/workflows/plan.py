"""Task Plan 的整体步骤（docs/plan/feedback-2026-11-01.md N-04；人工验收基线 Q55、Q56）。

- Planner 在非 fast 的第一轮给出 3～8 步（Task Plan 的 steps），之后每轮更新状态；fast 不给。
- Task 工作流是唯一写者：Planner 给出的步骤通过检查（steps_from）才替换 Task State 的 plan，不合格或没给（fast、light、
  保守规划、原地重跑）时保留现有的一份，不另建第二份状态。每次变了都推 plan_updated，载荷与 Task State 的 plan 相同。
- 结束时（finished）正在做的一步：done 的结局记为 done，其余结局记为 blocked；没有正在做的一步时不变。
- schema 是 defs.schema.json 的 plan_steps：3～8 步，最多一步 active；id 不重复、round 不晚于这一轮在这里检查。

纯函数，工作流里可用。
"""

from __future__ import annotations

import re
from typing import Any

MIN_STEPS = 3
MAX_STEPS = 8
TITLE_CHARS = 200
STATUSES = ("pending", "active", "done", "blocked")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _round_number(value: Any) -> int | None:
    """round 的整数值：模型的结构化输出经 protobuf Struct 后整数是 float（1.0），也认。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def step_problems(steps: Any, round_seq: int) -> list[str]:
    """不合格之处的说明（Planner 据此重问）；为空表示合格。"""
    if not isinstance(steps, list):
        return ["task_plan.steps 要是数组"]
    found: list[str] = []
    if not MIN_STEPS <= len(steps) <= MAX_STEPS:
        found.append(f"task_plan.steps 要有 {MIN_STEPS}～{MAX_STEPS} 步，现在是 {len(steps)} 步")
    seen: set[str] = set()
    active = 0
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            found.append(f"第 {i} 步不是对象")
            continue
        sid, title, status, rnd = step.get("id"), step.get("title"), step.get("status"), step.get("round")
        if not isinstance(sid, str) or not _STEP_ID.match(sid):
            found.append(f"第 {i} 步的 id 要是小写字母开头的 snake_case")
        elif sid in seen:
            found.append(f"步骤 id {sid} 重复")
        else:
            seen.add(sid)
        if not isinstance(title, str) or not title.strip() or len(title) > TITLE_CHARS:
            found.append(f"第 {i} 步的 title 要是 1～{TITLE_CHARS} 个字符")
        if status not in STATUSES:
            found.append(f"第 {i} 步的 status 只能是 {'、'.join(STATUSES)}")
        active += status == "active"
        rnd = _round_number(rnd)
        if rnd is None or rnd < 1:
            found.append(f"第 {i} 步的 round 要是从 1 开始的整数")
        elif rnd > round_seq:
            found.append(f"第 {i} 步的 round {rnd} 晚于本轮（第 {round_seq} 轮）")
        extra = sorted(set(step) - {"id", "title", "status", "round"})
        if extra:
            found.append(f"第 {i} 步多了 {'、'.join(extra)}")
    if active > 1:
        found.append(f"同一时刻最多一步 active，现在有 {active} 步")
    return found


def steps_from(task_plan: Any, round_seq: int) -> list[dict[str, Any]] | None:
    """Planner 这一轮给出的步骤：合格时返回规整后的副本（只留四个字段，title 去掉首尾空白），没给或不合格时返回 None。"""
    steps = task_plan.get("steps") if isinstance(task_plan, dict) else None
    if steps is None or step_problems(steps, round_seq):
        return None
    return [{"id": s["id"], "title": s["title"].strip(), "status": s["status"], "round": _round_number(s["round"])} for s in steps]


def snapshot(steps: list[dict[str, Any]], round_seq: int) -> dict[str, Any]:
    """Task State 的 plan 与 plan_updated 的内容：完整步骤、定下它的 Round、正在做的一步（有时）。"""
    plan: dict[str, Any] = {"steps": [dict(s) for s in steps], "round_seq": round_seq}
    current = next((s["id"] for s in steps if s["status"] == "active"), None)
    if current is not None:
        plan["current_step"] = current
    return plan


def finished(plan: dict[str, Any] | None, round_seq: int, *, done: bool) -> dict[str, Any] | None:
    """结束时的计划：正在做的一步按结局记为 done 或 blocked；没有计划或没有正在做的一步时返回 None（不变）。"""
    if not plan or not plan.get("current_step"):
        return None
    status = "done" if done else "blocked"
    steps = [{**s, "status": status, "round": round_seq} if s["status"] == "active" else dict(s) for s in plan["steps"]]
    return snapshot(steps, round_seq)


def brief_lines(plan: Any) -> list[str]:
    """给 Context Builder 的 brief 区（附录 G.1）：一行一个步骤。只随 Round 变，同一轮里每一步相同，不破坏提示缓存。"""
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        return []
    marks = {"pending": "待做", "active": "正在做", "done": "已完成", "blocked": "受阻"}
    lines = ["整体计划（Task Plan 的步骤；压缩上下文时保留）："]
    for i, s in enumerate(plan["steps"], start=1):
        if isinstance(s, dict):
            lines.append(f"{i}. [{marks.get(str(s.get('status')), s.get('status'))}] {s.get('title', '')}（{s.get('id', '')}）")
    return lines
