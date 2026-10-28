"""硬信号与升级台账（附录 B.6、B.8）：由代码计算，便宜且可靠。

台账从 Task State、本轮的 Stage 与结果、之前各 Round 的摘要（history）算出。history 的每一项是
{"round", "verdict", "step", "escalate"（escalate 的去向）, "failed_nodes"（Node id → 错误签名）}，由 history_entry 生成；
S-08 的样本只有 round、verdict 与 failed_nodes，缺的 step 按 Verdict 的默认步骤算。

纯函数，不做 I/O：工作流用 history_entry 记录每一轮。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

STEPS = (
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

HARD_SIGNALS = ("repetition", "stall", "budget_burn", "plan_drift", "context_health", "parallel_anomaly", "sample_divergence")
"""defs.schema.json 的 hard_signal。"""

FAILED_STATUSES = ("failed", "unknown")
"""算作失败的 Node 状态：失败，以及写操作结果未知（附录 K.4）。上游失败而跳过的不算。"""

PROGRESS_VERDICT = "continue"
"""有进展的 Round：本 Stage 的退出条件满足（有 Node 失败时不能判 continue，见 rules）。"""

SIGNATURE_LIMIT = 200
SAME_ERROR_MIN_LEN = 12
"""错误签名按“包含”判同一个错误时，短的那个至少这么长（“failed”“timeout”这样的不算）。"""
STALL_ROUNDS = 2
"""连续这么多个 Round 没有进展，记停滞。"""
DRIFT_REPLANS = 2
"""replan 这么多次，记计划漂移。"""

_DEFAULT_STEP = {
    "done": "none",
    "continue": "none",
    "repair": "repair_retry",
    "replan": "replan",
    "reroute": "replan_change_shape",
    "rollback": "rollback",
    "escalate": "escalate",
    "abort": "abort",
}


@dataclass(frozen=True)
class Ledger:
    """升级台账。

    node_failures：Node id → 失败过的 Round 数（含本轮）。
    since_progress：上次进展（continue）之后各级用了几次：repair_retry、repair_change_method、replan（含换形状）。
    per_task：整个 Task 里用了几次：replan_change_shape（含 reroute）、rollback、escalate_tier、escalate_human。
    rounds_without_progress：最近连续几个 Round 没有进展（不含本轮）。
    repeated：本轮失败、错误签名与上一轮相同的 Node（重复信号，附录 B.6）。
    merge_conflicts：合并扇出结果失败的 aggregate Node（并行异常，附录 B.7“扇出后子结果冲突”）。
    """

    node_failures: Mapping[str, int]
    since_progress: Mapping[str, int]
    per_task: Mapping[str, int]
    rounds_without_progress: int
    spent_usd: float
    budget_usd: float | None
    checkpoints: tuple[str, ...]
    signals: tuple[str, ...]
    repeated: tuple[str, ...] = ()
    merge_conflicts: tuple[str, ...] = ()

    def spent_ratio(self) -> float | None:
        """已花的预算占比；没有预算时为 None。"""
        if not self.budget_usd:
            return None
        return self.spent_usd / self.budget_usd


def step_of(verdict: Mapping[str, Any]) -> str:
    """Verdict 对应的升级步骤（STEPS 之一）。"""
    name = verdict.get("verdict")
    if name == "repair":
        return "repair_change_method" if (verdict.get("repair") or {}).get("strategy") == "change_method" else "repair_retry"
    if name == "replan":
        return "replan_change_shape" if (verdict.get("replan") or {}).get("change_shape") else "replan"
    if name in _DEFAULT_STEP:
        return _DEFAULT_STEP[str(name)]
    raise ValueError(f"未知的 Verdict {name!r}")


def error_signature(text: str) -> str:
    """错误签名：数字换成 #，空白归一，截短。同一个错误换了行号或计数仍是同一个签名。"""
    return " ".join(re.sub(r"\d+", "#", str(text)).split())[:SIGNATURE_LIMIT]


def same_error(a: str, b: str) -> bool:
    """两个错误签名是不是同一个错误：相同，或一个包含另一个（质量包装失败时会在原错误前加上说明）。太短的不按包含算。"""
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= SAME_ERROR_MIN_LEN and short in long_


def is_failed(result: Mapping[str, Any]) -> bool:
    return result.get("status") in FAILED_STATUSES


def failed_nodes(results: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """本轮失败的 Node → 错误签名，按 Node id 排序。"""
    return {nid: error_signature(r.get("error") or r.get("failure_class") or "failed") for nid, r in sorted(results.items()) if is_failed(r)}


def history_entry(round_seq: int, verdict: Mapping[str, Any], results: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """一轮的摘要，交给之后的 Reviewer。"""
    entry: dict[str, Any] = {"round": round_seq, "verdict": verdict["verdict"], "step": step_of(verdict)}
    if verdict["verdict"] == "escalate":
        entry["escalate"] = str((verdict.get("escalate") or {}).get("target") or "human")
    failed = failed_nodes(results)
    if failed:
        entry["failed_nodes"] = failed
    return entry


def entry_step(entry: Mapping[str, Any]) -> str:
    return str(entry.get("step") or _DEFAULT_STEP.get(str(entry.get("verdict")), "none"))


def _zero_since() -> dict[str, int]:
    return {"repair_retry": 0, "repair_change_method": 0, "replan": 0}


def ledger(task_state: Mapping[str, Any], stage: Mapping[str, Any], results: Mapping[str, Mapping[str, Any]], history: list[Mapping[str, Any]]) -> Ledger:
    current = failed_nodes(results)
    failures: Counter[str] = Counter()
    for entry in history:
        failures.update((entry.get("failed_nodes") or {}).keys())
    failures.update(current.keys())

    since = _zero_since()
    per_task = {"replan_change_shape": 0, "rollback": 0, "escalate_tier": 0, "escalate_human": 0}
    replans = 0
    for entry in history:
        if entry.get("verdict") == PROGRESS_VERDICT:
            since = _zero_since()
            continue
        step = entry_step(entry)
        if step in ("repair_retry", "repair_change_method"):
            since[step] += 1
        elif step in ("replan", "replan_change_shape"):
            since["replan"] += 1
            replans += 1
            if step == "replan_change_shape":
                per_task["replan_change_shape"] += 1
        elif step == "rollback":
            per_task["rollback"] += 1
        elif step == "escalate":
            target = "tier" if entry.get("escalate") == "tier" else "human"
            per_task[f"escalate_{target}"] += 1
    stalled = 0
    for entry in reversed(history):
        if entry.get("verdict") == PROGRESS_VERDICT:
            break
        stalled += 1

    rounds = list(task_state.get("rounds") or [])
    spent = sum(float(r.get("cost_usd") or 0.0) for r in rounds)
    budget = (task_state.get("constraints") or {}).get("budget_usd")
    checkpoints = tuple(f"round_{r['seq']}" for r in rounds if r.get("verdict") == PROGRESS_VERDICT and r.get("status", "completed") == "completed")

    signals: set[str] = set()
    last_failed = {nid: error_signature(sig) for nid, sig in ((history[-1].get("failed_nodes") or {}) if history else {}).items()}
    repeated = tuple(sorted(nid for nid, sig in current.items() if nid in last_failed and same_error(last_failed[nid], sig)))
    if repeated:
        signals.add("repetition")
    if any("repeated_error" in str(r.get("error", "")) for r in results.values() if is_failed(r)):
        signals.add("repetition")
    if current and stalled >= STALL_ROUNDS:
        signals.add("stall")
    if replans >= DRIFT_REPLANS:
        signals.add("plan_drift")
    merges = _merge_conflicts(stage, results)
    if merges or _parallel_anomaly(stage, results):
        signals.add("parallel_anomaly")

    return Ledger(
        node_failures=dict(sorted(failures.items())),
        since_progress=since,
        per_task=per_task,
        rounds_without_progress=stalled,
        spent_usd=spent,
        budget_usd=float(budget) if budget else None,
        checkpoints=checkpoints,
        signals=tuple(sorted(signals)),
        repeated=repeated,
        merge_conflicts=merges,
    )


def _empty(value: Any) -> bool:
    return value is None or value == {} or value == [] or value == ""


def _merge_conflicts(stage: Mapping[str, Any], results: Mapping[str, Mapping[str, Any]]) -> tuple[str, ...]:
    """失败的 aggregate Node，它合并的是至少两个互不依赖、都成功了的上游（扇出的分支）。"""
    nodes = {str(n["id"]): n for n in stage.get("nodes") or []}
    out = []
    for nid, node in sorted(nodes.items()):
        result = results.get(nid, {})
        if not is_failed(result) or "aggregate" not in (node.get("role"), result.get("role")):
            continue
        ups = [str(u) for u in node.get("after") or [] if results.get(str(u), {}).get("status") == "succeeded"]
        branches = [u for u in ups if not set(nodes.get(u, {}).get("after") or []) & set(ups)]
        if len(branches) >= 2:
            out.append(nid)
    return tuple(out)


def _parallel_anomaly(stage: Mapping[str, Any], results: Mapping[str, Mapping[str, Any]]) -> bool:
    """互不依赖的 sub_agent 至少两个，其中有的成功了却交回空结果。子结果互相矛盾要看内容，交给 Progress Check。"""
    nodes = [n for n in stage.get("nodes") or [] if n.get("kind") == "goal" and n.get("mode") == "sub_agent"]
    if len(nodes) < 2:
        return False
    ids = {str(n["id"]) for n in nodes}
    parallel = [n for n in nodes if not ids & set(n.get("after") or [])]
    if len(parallel) < 2:
        return False
    return any(results.get(str(n["id"]), {}).get("status") == "succeeded" and _empty(results[str(n["id"])].get("result")) for n in parallel)
