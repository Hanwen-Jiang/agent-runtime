"""Task 工作流按 Verdict 的去向走时用到的纯函数（S-10；附录 B.4、B.8）。工作流代码必须确定：不读时间、不做 I/O。

- 原地重试与换更强的 Tier：下一轮不回 Planner，重跑同一个 Stage 里出问题的部分（要修的 Node 及其下游，
  以及本轮没有成功的 Node）；其余成功的 Node 结果沿用，不重跑。错误与指导交给重跑的 Node：Atomic Node 作为
  status 区的注入，Goal Node 写进它的 goal。
- rollback：Task State 的事实、假设、待查问题、产物与里程碑退回检查点（以 continue 结束的 Round）时的样子；
  Round 记录本身不回退。外部的改动由下一轮的 Planner 安排撤回。
- abort 与问人后停止：交付现有结果，说清完成了什么、没完成什么。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from agentruntime_worker.activities.types import StageResult
from agentruntime_worker.reviewer.rules import repair_targets
from agentruntime_worker.reviewer.signals import FAILED_STATUSES
from agentruntime_worker.workflows import stage_plan as sp

REVIEW_QUESTION_ID = "_review"
"""Reviewer 问人时工作流里的问题 id：不合 Node id 的格式，不会与任何 Node 冲突。"""
SNAPSHOT_KEYS = ("facts", "assumptions", "open_questions", "artifacts", "milestones")


@dataclass
class Rerun:
    """下一轮不经 Planner 重跑的 Stage：沿用的结果与给重跑 Node 的注入。"""

    stage: dict[str, Any]
    carried: dict[str, StageResult] = field(default_factory=dict)
    injections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def review_output(raw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """review_round 的结果：S-10 是 {"verdict", "state_update"}，S-03 的桩只返回 Verdict 本身。"""
    if isinstance(raw, Mapping) and isinstance(raw.get("verdict"), Mapping):
        return dict(raw["verdict"]), dict(raw.get("state_update") or {})
    return dict(raw), {}


def stage_summary(stage: Mapping[str, Any]) -> dict[str, Any]:
    nodes = list(stage.get("nodes") or [])
    return {"seq": stage.get("seq"), "goal": stage.get("goal"), "nodes": len(nodes), "tiers": sorted({str(n["tier"]) for n in nodes if n.get("tier")})}


def snapshot(task_state: Mapping[str, Any]) -> dict[str, Any]:
    return {k: copy.deepcopy(task_state.get(k, [])) for k in SNAPSHOT_KEYS}


def restore(task_state: dict[str, Any], snap: Mapping[str, Any] | None) -> None:
    if snap is None:
        return
    for k in SNAPSHOT_KEYS:
        task_state[k] = copy.deepcopy(snap.get(k, []))


def checkpoint_round(verdict: Mapping[str, Any]) -> int | None:
    """rollback.checkpoint（形如 round_3）对应的 Round 序号。"""
    name = str((verdict.get("rollback") or {}).get("checkpoint", ""))
    number = name.removeprefix("round_")
    return int(number) if name.startswith("round_") and number.isdigit() else None


def rerun(stage: Mapping[str, Any], results: Mapping[str, StageResult], verdict: Mapping[str, Any], seq: int) -> Rerun:
    nodes = {str(n["id"]): n for n in stage.get("nodes") or []}
    as_dicts = {nid: asdict(r) for nid, r in results.items()}
    tier = ""
    if verdict["verdict"] == "repair":
        targets = [n for n in verdict["repair"].get("nodes") or [] if n in nodes]
        guidance = str(verdict["repair"].get("guidance") or "")
    else:
        esc = verdict["escalate"]
        tier = str(esc.get("tier") or "")
        guidance = ""
        targets = repair_targets(nodes, as_dicts) or sorted(nid for nid, n in nodes.items() if n.get("executor") == "model" or n.get("kind") == "goal")
    failed = sorted(nid for nid, r in results.items() if r.status in FAILED_STATUSES)
    targets = sorted(set(targets or failed or nodes))
    redo = set(targets) | sp.descendants(stage, targets) | {nid for nid in nodes if nid not in results or results[nid].status != "succeeded"}
    carried = {nid: results[nid] for nid in sorted(nodes) if nid not in redo}
    errors = "；".join(f"{nid}：{results[nid].error}" for nid in sorted(redo) if nid in results and results[nid].status in FAILED_STATUSES and results[nid].error)
    note = "；".join(p for p in (f"上一次失败：{errors}" if errors else "", f"Reviewer 的指导：{guidance}" if guidance else "") if p) or "上一次没有通过，再做一次"
    new_stage = copy.deepcopy(dict(stage))
    new_stage["seq"] = seq
    injections: dict[str, list[dict[str, Any]]] = {}
    for node in new_stage.get("nodes") or []:
        if node["id"] not in targets:
            continue
        if node.get("kind") == "goal":
            node["goal"] = f"{node.get('goal', '')}（重做：{note}）"
        else:
            injections[node["id"]] = [{"zone": "status", "content": f"重做：{note}"}]
            if tier and node.get("executor") == "model":
                node["tier"] = tier
    return Rerun(stage=new_stage, carried=carried, injections=injections)


def stopped(results: Mapping[str, StageResult], reason: str) -> dict[str, Any]:
    """问人后停止（或问人超时）：按 abort 收尾的 Verdict。"""
    completed = [f"{nid} 已完成" for nid, r in sorted(results.items()) if r.status == "succeeded"]
    not_completed = [f"{nid}：{r.error or r.status}" for nid, r in sorted(results.items()) if r.status != "succeeded"]
    return {"verdict": "abort", "reason": reason, "abort": {"completed": completed, "not_completed": not_completed or ["交付"]}}


def abort_body(verdict: Mapping[str, Any], partial: Any = None) -> dict[str, Any]:
    """abort 的交付：原因、完成了什么、没完成什么，以及最近一个成功的 deliver 节点的结果（有的话）。"""
    details = verdict.get("abort") or {}
    body: dict[str, Any] = {
        "reason": verdict.get("reason", ""),
        "completed": list(details.get("completed") or []),
        "not_completed": list(details.get("not_completed") or []),
    }
    if partial is not None:
        body["partial"] = partial
    return body
