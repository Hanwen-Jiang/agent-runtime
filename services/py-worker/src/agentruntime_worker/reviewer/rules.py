"""升级顺序与触发规则（附录 B.8）：代码判定 floor 与次数上限，模型的提议只会被往上抬。

floor：从台账算出至少要升级到哪一步；预算规则与硬信号 environment（工作区或执行环境本身不可用，TMP 计划
eval-observability-and-runtime-fixes 第 2 节）另限定可选的 Verdict。除了 B.8 的次数规则，还有附录 B.6、B.7 里代码算得出的三条
（2026-10-28 真模型实验第 3.2 条）：本轮已在 Node 里重试过、又是上一轮的同一个错误，replan；扇出后合并失败，replan；
质量包装用满轮数仍没通过 judge 的产出（Stage Runner 记成 succeeded、quality.passed = false），不能 continue 或 done，换方法。
enforce：拿模型的提议（或没有提议）依次做三件事——把细节改成合法的（done 只来自本轮成功的 deliver 节点、Progress Check
自己答了验收条件没满足的不能 done、
Node 必须属于本 Stage、问人只给三种回复、rollback 只退到已有的检查点、reroute 只换到本租户 Catalog 里且不超出
effect 上限的 Recipe）；低于 floor 的抬到 floor；用满上限的一级换到上一级。纯函数。
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agentruntime_worker.reviewer.signals import HARD_SIGNALS, STEPS, Ledger, is_failed, step_of
from agentruntime_worker.workflows import stage_plan as sp

HUMAN_REPLIES = ("continue_as_is", "replan_with_guidance", "stop_and_wrap_up")
"""Reviewer 升级问人时限定的三种回复（设计书 4.11）。"""

JUDGING_ROLES = ("judge", "gate")
_RANK = {s: i for i, s in enumerate(STEPS)}
_LABEL = {
    "none": "继续",
    "continue": "继续",
    "repair_retry": "原地重试",
    "repair_change_method": "换方法",
    "replan": "重规划",
    "replan_change_shape": "换 Stage 形状",
    "rollback": "退回检查点",
    "escalate_tier": "换更强的 Tier",
    "escalate_human": "问人",
    "abort": "交付现有结果并收尾",
}


@dataclass(frozen=True)
class Caps:
    """各级的次数上限（防震荡）。前三项按上次进展之后计，其余按整个 Task 计。"""

    repair_retry: int = 2
    repair_change_method: int = 2
    replan: int = 2
    replan_change_shape: int = 1
    rollback: int = 1
    escalate_tier: int = 1
    escalate_human: int = 2


@dataclass(frozen=True)
class Thresholds:
    budget_alarm_spent: float = 0.8
    """预算用掉这么多（比例）……"""
    budget_alarm_completion: float = 0.5
    """……而完成度不到这么多时，缩小范围或问人。"""


@dataclass(frozen=True)
class Floor:
    """代码判定的下限：至少要升级到 step；allowed 不为空时只能在这几种 Verdict 里选（预算规则、硬信号 environment、regressions）。

    code、params 是决定 step 的那一条规则的代码与参数；reason_code、reason_params 是限定 allowed 的那一条的（TMP 计划第 5 节）。
    """

    step: str
    reasons: tuple[str, ...] = ()
    allowed: frozenset[str] | None = None
    signals: tuple[str, ...] = ()
    reason_code: str = ""
    reason_params: Mapping[str, Any] = field(default_factory=dict)
    code: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewCatalog:
    """reroute 的校验：本租户 Catalog 里的 Recipe → 风险等级，与 Task 的 effect 上限。"""

    recipes: Mapping[str, str] = field(default_factory=dict)
    effect_ceiling: str = "read"


@dataclass(frozen=True)
class Decision:
    verdict: dict[str, Any]
    raised: bool
    """代码改了模型提议的 Verdict 或升级步骤。"""
    reasons: tuple[str, ...] = ()


# --- floor ---


def floor(ledger: Ledger, results: Mapping[str, Mapping[str, Any]], *, completion: float | None, thresholds: Thresholds = Thresholds()) -> Floor:
    step = "none"
    reasons: list[str] = []
    signals: set[str] = set()
    step_code: tuple[str, dict[str, Any]] = ("", {})

    def need(target: str, why: str, code: str, **params: Any) -> None:
        nonlocal step, step_code
        reasons.append(why)
        if _RANK[target] > _RANK[step]:
            step = target
            step_code = (code, params)

    failed = sorted(nid for nid, r in results.items() if is_failed(r))
    if failed:
        need("repair_retry", f"本轮有 Node 失败（{'、'.join(failed)}）：本 Stage 的退出条件不算满足，不能判 continue 或 done", "rule_node_failed", nodes=failed)
        for nid in failed:
            times = ledger.node_failures.get(nid, 1)
            if times >= 3:
                need("replan", f"同一 Node（{nid}）已失败 {times} 次：重规划（附录 B.8）", "rule_failed_thrice", node=nid, times=times)
            elif times == 2:
                need("repair_change_method", f"同一 Node（{nid}）已失败 2 次：换方法，不能原地重试（附录 B.8）", "rule_failed_twice", node=nid, times=2)
        denied = [nid for nid in failed if results[nid].get("failure_class") == "denied"]
        if denied:
            need("replan", f"{'、'.join(denied)} 被拒绝（denied）：原样重试没有用，换做法或问人", "rule_denied", nodes=denied)
        for nid in ledger.repeated:
            tries = retries_within_round(results.get(nid, {}))
            if tries >= 2:
                need(
                    "replan", f"{nid} 本轮内已重试 {tries} 次，仍是和上一轮相同的错误：问题在上游，停止修复，重规划（附录 B.7）",
                    "rule_same_error_after_retry", node=nid, retries=tries,
                )
        for nid in ledger.merge_conflicts:
            need("replan", f"{nid} 合并扇出的结果失败：耦合度被低估，收回单一写者，或先定共享接口再扇出（附录 B.7）", "rule_merge_conflict", node=nid)
        replans = ledger.since_progress.get("replan", 0)
        if replans >= 2:
            need("replan_change_shape", f"replan {replans} 次仍无进展：换 Stage 形状、换流程或问人（附录 B.8）", "rule_replan_no_progress", replans=replans)

    unverified = unverified_nodes(results)
    if unverified:
        need(
            "repair_change_method",
            f"{'、'.join(unverified)} 的质量包装用满轮数仍没通过 judge：产出没有经过验证，不能 continue 或 done；"
            "已经原地重试过，换方法（引入外部判据或独立 judge，附录 B.7）",
            "rule_unverified_quality",
            nodes=unverified,
        )

    allowed: set[str] | None = None
    limit: tuple[str, dict[str, Any]] = ("", {})

    def restrict(outs: set[str], why: str, code: str, **params: Any) -> None:
        nonlocal allowed, limit
        reasons.append(why)
        allowed = set(outs) if allowed is None else allowed & outs
        limit = (code, params)

    # TMP 计划第 7 节：judge 发现之前已有的产物被这一轮改坏了：至少退回检查点（没有检查点时问人），不能继续、修补或交付。
    broken = sorted({str(g.get("artifact")) for r in results.values() for g in (r.get("regressions") or []) if isinstance(g, Mapping) and g.get("artifact")})
    if broken:
        need("rollback", f"judge 发现被这一轮改坏的既有产物（{'、'.join(broken)}）：退回最近的检查点", "rule_regression", artifacts=broken)
        restrict({"rollback", "escalate", "abort"}, "既有产物被改坏：只能退回检查点、问人或收尾", "rule_regression", artifacts=broken)

    ratio = ledger.spent_ratio()
    if ratio is not None:
        if ratio >= 1.0:
            signals.add("budget_burn")
            restrict({"escalate", "abort"}, f"预算已用 {ratio:.0%}：只能问人，或交付现有结果收尾", "rule_budget_spent", spent_ratio=round(ratio, 2))
        elif completion is not None and ratio >= thresholds.budget_alarm_spent and completion < thresholds.budget_alarm_completion:
            signals.add("budget_burn")
            restrict(
                {"replan", "escalate", "abort"}, f"预算已用 {ratio:.0%}，完成度只有 {completion:.0%}：缩小范围或问人（附录 B.8）",
                "rule_budget_alarm", spent_ratio=round(ratio, 2), completion=round(completion, 2),
            )
        elif completion and ledger.budget_usd and ledger.spent_usd / completion > ledger.budget_usd:
            signals.add("budget_burn")
            reasons.append("按已花的预算与完成度外推，预计会超出预算")
    empty = sorted(nid for nid, r in results.items() if "environment" in (r.get("signals") or []))
    if empty:
        # 列工作区根得到空目录：换做法、重规划都没有用。交付“工作区是空的”这类答复是对的，所以 done 仍可以。
        signals.add("environment")
        restrict(
            {"done", "escalate", "abort"},
            f"{'、'.join(empty)} 列工作区根得到空目录：工作区或执行环境本身不可用，换做法没有用；只能交付现有答复、问人或收尾",
            "workspace_empty",
            nodes=empty,
        )
    return Floor(
        step=step,
        reasons=tuple(reasons),
        allowed=frozenset(allowed) if allowed is not None else None,
        signals=tuple(sorted(signals)),
        reason_code=limit[0],
        reason_params=limit[1],
        code=step_code[0],
        params=step_code[1],
    )


# --- enforce ---


def enforce(
    proposal: Mapping[str, Any] | None,
    *,
    floor: Floor,
    ledger: Ledger,
    stage: Mapping[str, Any],
    results: Mapping[str, Mapping[str, Any]],
    catalog: ReviewCatalog | None = None,
    caps: Caps = Caps(),
    escalate_tier: str = "strong@high",
    verification: Mapping[str, Any] | None = None,
) -> Decision:
    rules = _Rules(floor, ledger, stage, results, catalog, caps, escalate_tier, verification)
    reasons: list[str] = []
    if proposal is None:
        verdict = rules.fallback()
        reasons.append("没有可用的模型判断：按规则判定")
        before: tuple[str, str] | None = None
    else:
        verdict = copy.deepcopy(dict(proposal))
        before = (str(verdict["verdict"]), step_of(verdict))
    verdict = rules.valid(verdict, reasons)
    if _RANK[step_of(verdict)] < _RANK[floor.step]:
        reasons.extend(floor.reasons)
        verdict = rules.make(floor.step, verdict, list(floor.reasons), code=floor.code, params=floor.params)
    verdict = rules.settle(verdict, reasons)
    if proposal is None and "reason_code" not in verdict:
        verdict["reason_code"] = "rule_fallback"
    proposed = {str(s) for s in (proposal or {}).get("signals") or [] if s in HARD_SIGNALS}
    signals = sorted(set(ledger.signals) | set(floor.signals) | proposed)
    if signals:
        verdict["signals"] = signals
    else:
        verdict.pop("signals", None)
    after = (str(verdict["verdict"]), step_of(verdict))
    return Decision(verdict=verdict, raised=before is not None and after != before, reasons=tuple(reasons))


class _Rules:
    def __init__(
        self,
        floor: Floor,
        ledger: Ledger,
        stage: Mapping[str, Any],
        results: Mapping[str, Mapping[str, Any]],
        catalog: ReviewCatalog | None,
        caps: Caps,
        escalate_tier: str,
        verification: Mapping[str, Any] | None = None,
    ) -> None:
        self.verification = verification
        self.floor = floor
        self.ledger = ledger
        self.results = results
        self.catalog = catalog
        self.caps = caps
        self.escalate_tier = escalate_tier
        self.nodes = {str(n["id"]): n for n in stage.get("nodes") or []}
        self.failed = sorted(nid for nid, r in results.items() if is_failed(r))
        self.succeeded = sorted(nid for nid, r in results.items() if r.get("status") == "succeeded" and nid in self.nodes)

    # --- 规则判定（没有模型的提议时） ---

    def fallback(self) -> dict[str, Any]:
        delivers = self.delivers()
        if not self.failed and delivers:
            return {"verdict": "done", "reason": "各 Node 都成功，deliver 节点给出了结果", "done": {"deliver_node": delivers[0]}}
        if not self.failed:
            return {"verdict": "continue", "reason": "各 Node 都成功，本轮还没有交付"}
        return self.make(self.floor.step, None, list(self.floor.reasons), code=self.floor.code, params=self.floor.params)

    def unproven_done(self) -> tuple[str, str] | None:
        """判 done 却明显没做完（TMP 计划第 11.15 节）：能改文件却没改，或改完没运行过任何命令。各种情况合计只拦一次：有的任务确实不用改、
        改完也没法跑，再判 done 就算数。最后一次命令失败不拦：项目可能本来就有失败的测试，由 Reviewer 判断。"""
        record = self.verification
        if not record or not record.get("write_capable") or int(record.get("done_blocked") or 0) >= 1:
            return None
        if not record.get("wrote"):
            return "能改文件，却一个文件都没有改就判 done：先确认是不是真的不用改", "rule_done_without_changes"
        if not record.get("ran_after_write"):
            return "改完之后没有运行过任何命令：先验证再交付", "rule_done_unverified"
        return None

    # --- 细节的合法性 ---

    def valid(self, verdict: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
        kind = verdict["verdict"]
        if kind == "done" and (verdict.get("progress_check") or {}).get("criteria_met") is False:
            why = "Progress Check 自己答了验收条件没满足，却判 done：不能交付"
            reasons.append(why)
            return self.make("continue" if not self.failed else self.floor.step, verdict, [why], code="rule_criteria_not_met")
        if kind == "done":
            delivers = self.delivers()
            if not delivers:
                why = "本轮没有成功的 deliver 节点：是否完成看产出物，还不能判 done"
                reasons.append(why)
                return self.make("continue" if not self.failed else self.floor.step, verdict, [why], code="rule_no_deliver")
            unproven = self.unproven_done()
            if unproven is not None:
                why, code = unproven
                reasons.append(why)
                return self.make("continue" if not self.failed else self.floor.step, verdict, [why], code=code)
            if verdict["done"].get("deliver_node") not in delivers:
                reasons.append(f"done 的交付改为本轮成功的 deliver 节点 {delivers[0]}")
                verdict["done"] = {"deliver_node": delivers[0]}
            chosen = str(verdict["done"]["deliver_node"])
            bad = deliverable_problem(self.results[chosen].get("result"))
            if bad is not None:
                # docs/plan/feedback-2026-11-01.md N-02：交付是空的或带着没解析的引用，不管是不是模型判的都不能交付。
                why, code, params = bad
                reasons.append(why)
                out = self.make("replan" if not self.failed else self.floor.step, verdict, [why], code=code, params={"node": chosen, **params})
                if out["verdict"] == "replan" and chosen in (out["replan"].get("keep_nodes") or []):
                    out["replan"]["keep_nodes"] = [n for n in out["replan"]["keep_nodes"] if n != chosen]
                return out
        elif kind == "repair":
            nodes = [n for n in dict.fromkeys(verdict["repair"].get("nodes") or []) if n in self.nodes]
            if not nodes:
                nodes = self.repair_nodes()
                reasons.append(f"repair 的 Node 不在本 Stage 里，改为 {'、'.join(nodes)}")
            verdict["repair"]["nodes"] = nodes
        elif kind == "replan":
            details = verdict.setdefault("replan", {})
            if "keep_nodes" in details:
                keep = [n for n in dict.fromkeys(details["keep_nodes"]) if n in self.succeeded]
                if keep != list(details["keep_nodes"]):
                    reasons.append("只能保留本轮成功的 Node")
                details["keep_nodes"] = keep
        elif kind == "rollback":
            checkpoint = verdict["rollback"]["checkpoint"]
            if checkpoint not in self.ledger.checkpoints:
                why = f"没有检查点 {checkpoint}（可退回的：{'、'.join(self.ledger.checkpoints) or '无'}）：退不回去，往上一级"
                reasons.append(why)
                return self.make("escalate", verdict, [why], code="rule_no_checkpoint", params={"checkpoint": str(checkpoint)})
        elif kind == "reroute":
            recipe = verdict["reroute"]["recipe"]
            recipes = self.catalog.recipes if self.catalog else {}
            if recipe not in recipes:
                why = f"Catalog 里没有 Recipe {recipe}：不能换过去，改为同级的换 Stage 形状"
                reasons.append(why)
                return self.make("replan_change_shape", verdict, [why], code="rule_unknown_recipe", params={"recipe": str(recipe)})
            assert self.catalog is not None
            risk, ceiling = recipes[recipe], self.catalog.effect_ceiling
            if sp.effect_rank(risk) > sp.effect_rank(ceiling):
                why = f"{recipe} 的风险 {risk} 超出本 Task 的 effect 上限 {ceiling}：先问人授权"
                reasons.append(why)
                question = f"任务更适合换到 {recipe} 的流程，但它的风险等级 {risk} 超出本 Task 允许的 {ceiling}。授权后按它继续、带指导重规划，还是停止？"
                params = {"recipe": str(recipe), "risk": str(risk), "ceiling": str(ceiling)}
                return self.make("escalate_human", verdict, [why], question=question, code="rule_recipe_over_ceiling", params=params)
        elif kind == "escalate":
            esc = verdict["escalate"]
            if esc.get("target") == "human":
                self.human_details(esc, [str(verdict.get("reason") or "")])
        return verdict

    # --- 预算限定与次数上限 ---

    def settle(self, verdict: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
        for _ in range(len(STEPS) * 2):
            allowed = self.floor.allowed
            kind = verdict["verdict"]
            if allowed is not None and (kind not in allowed or (kind == "escalate" and verdict["escalate"].get("target") == "tier")):
                reasons.extend(self.floor.reasons)
                verdict = self.make("escalate_human", verdict, list(self.floor.reasons), code=self.floor.reason_code, params=self.floor.reason_params)
                continue
            used, cap, nxt = self.cap_of(verdict)
            if cap is not None and used >= cap:
                key = self.cap_key(verdict)
                why = f"{_LABEL[key]}已用满上限（{used}/{cap}）：升一级"
                reasons.append(why)
                verdict = self.make(nxt, verdict, [why], code="rule_cap_reached", params={"step": key, "used": used, "cap": cap, "next": nxt})
                continue
            break
        if verdict["verdict"] == "replan" and self.floor.allowed is not None and not verdict["replan"].get("stuck_reason"):
            verdict["replan"]["stuck_reason"] = "；".join(self.floor.reasons)
        return verdict

    def cap_key(self, verdict: Mapping[str, Any]) -> str:
        step = step_of(verdict)
        if step == "escalate":
            return "escalate_tier" if verdict["escalate"].get("target") == "tier" else "escalate_human"
        return step

    def cap_of(self, verdict: Mapping[str, Any]) -> tuple[int, int | None, str]:
        key = self.cap_key(verdict)
        since, per_task, caps = self.ledger.since_progress, self.ledger.per_task, self.caps
        table: dict[str, tuple[int, int, str]] = {
            "repair_retry": (since.get("repair_retry", 0), caps.repair_retry, "repair_change_method"),
            "repair_change_method": (since.get("repair_change_method", 0), caps.repair_change_method, "replan"),
            "replan": (since.get("replan", 0), caps.replan, "replan_change_shape"),
            "replan_change_shape": (per_task.get("replan_change_shape", 0), caps.replan_change_shape, "escalate"),
            "rollback": (per_task.get("rollback", 0), caps.rollback, "escalate"),
            "escalate_tier": (per_task.get("escalate_tier", 0), caps.escalate_tier, "escalate_human"),
            "escalate_human": (per_task.get("escalate_human", 0), caps.escalate_human, "abort"),
        }
        if key not in table:
            return 0, None, key
        return table[key]

    # --- 构造 Verdict ---

    def make(
        self, step: str, base: Mapping[str, Any] | None, why: list[str], *, question: str = "", code: str = "", params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """按升级步骤构造 Verdict；沿用原提议里还适用的部分（Progress Check、layer、repair 的 Node 与指导、问题等）。
        code、params 是改写理由的规则的代码与参数（TMP 计划第 5 节），客户端按它翻译，reason 作后备。"""
        base = base or {}
        if step == "escalate":
            step = self.escalate_step()
        if step == "rollback" and not self.ledger.checkpoints:
            # 没有检查点可退：问人（TMP 计划第 7 节）。
            step = "escalate_human"
        label = _LABEL.get(step, step)
        text = "；".join(w for w in why if w)
        reason = f"{base['reason']}；按规则改为{label}：{text}" if base.get("reason") else (text or label)
        out: dict[str, Any] = {"reason": reason}
        if code:
            out["reason_code"] = code
            if params:
                out["reason_params"] = dict(params)
        for key in ("layer", "progress_check"):
            if key in base:
                out[key] = copy.deepcopy(base[key])
        if step in ("repair_retry", "repair_change_method"):
            old = base.get("repair") if base.get("verdict") == "repair" else None
            nodes = [n for n in (old or {}).get("nodes") or [] if n in self.nodes] or self.repair_nodes()
            repair: dict[str, Any] = {"nodes": nodes, "strategy": "retry_with_error" if step == "repair_retry" else "change_method"}
            if old and old.get("guidance"):
                repair["guidance"] = old["guidance"]
            out.update(verdict="repair", repair=repair)
        elif step in ("replan", "replan_change_shape"):
            old = base.get("replan") if base.get("verdict") == "replan" else None
            details: dict[str, Any] = {"change_shape": step == "replan_change_shape", "stuck_reason": (old or {}).get("stuck_reason") or text or label}
            keep = [n for n in (old or {}).get("keep_nodes") or [] if n in self.succeeded] if old and "keep_nodes" in old else self.default_keep()
            if keep:
                details["keep_nodes"] = keep
            out.update(verdict="replan", replan=details)
        elif step == "escalate_tier":
            out.update(verdict="escalate", escalate={"target": "tier", "tier": self.escalate_tier, "state_summary": self.summary()})
        elif step == "escalate_human":
            old = base.get("escalate") if base.get("verdict") == "escalate" else None
            esc: dict[str, Any] = {"target": "human"}
            if question:
                esc["question"] = question
            elif old and old.get("target") == "human" and old.get("question"):
                esc["question"] = old["question"]
            if old and old.get("state_summary"):
                esc["state_summary"] = old["state_summary"]
            self.human_details(esc, why)
            out.update(verdict="escalate", escalate=esc)
        elif step == "rollback":
            out.update(verdict="rollback", rollback={"checkpoint": self.ledger.checkpoints[-1]})
        elif step == "abort":
            old = base.get("abort") if base.get("verdict") == "abort" else None
            out.update(verdict="abort", abort=copy.deepcopy(old) if old else self.abort_details())
        elif step == "continue" or step == "none":
            out.update(verdict="continue")
        else:
            raise ValueError(f"规则不会自动走到 {step}")
        return out

    def escalate_step(self) -> str:
        per_task, caps = self.ledger.per_task, self.caps
        if self.floor.allowed is None and per_task.get("escalate_tier", 0) < caps.escalate_tier:
            return "escalate_tier"
        if per_task.get("escalate_human", 0) < caps.escalate_human:
            return "escalate_human"
        return "abort"

    def human_details(self, esc: dict[str, Any], why: list[str]) -> None:
        if not esc.get("question"):
            text = "；".join(w for w in why if w) or "卡住了，需要人来决定下一步"
            esc["question"] = f"{text}。原样继续、带指导重规划，还是停止并用现有结果收尾？"
        esc["options"] = list(HUMAN_REPLIES)
        if not esc.get("state_summary"):
            esc["state_summary"] = self.summary()

    # --- 默认的细节 ---

    def delivers(self) -> list[str]:
        return [nid for nid in self.succeeded if self.results[nid].get("role") == "deliver" or self.nodes[nid].get("role") == "deliver"]

    def repair_nodes(self) -> list[str]:
        return repair_targets(self.nodes, self.results) or (self.failed or sorted(self.nodes)[:1])

    def default_keep(self) -> list[str]:
        """代码抬到 replan 时保留的 Node：本轮成功的，减去被失败的 judge 否掉的上游产出（它正是要重做的）、合并不了的扇出分支，
        以及质量包装没通过的产出。"""
        rejected: set[str] = set(unverified_nodes(self.results))
        for nid in self.failed:
            if _judging(nid, self.nodes, self.results) or nid in self.ledger.merge_conflicts:
                rejected.update(self.nodes.get(nid, {}).get("after") or [])
        return [n for n in self.succeeded if n not in rejected]

    def summary(self) -> str:
        parts = [f"本轮成功：{'、'.join(self.succeeded) or '无'}", f"失败：{'、'.join(self.failed) or '无'}"]
        ratio = self.ledger.spent_ratio()
        if ratio is not None:
            parts.append(f"预算已用 {ratio:.0%}")
        return "；".join(parts)

    def abort_details(self) -> dict[str, list[str]]:
        completed = [f"{nid} 已完成" for nid in self.succeeded]
        not_completed = [f"{nid}：{self.results[nid].get('error') or '失败'}" for nid in self.failed]
        not_completed += [f"{nid}（上游没有成功）" for nid, r in sorted(self.results.items()) if r.get("status") == "skipped"]
        return {"completed": completed, "not_completed": not_completed or ["交付"]}


# 交付里没解析的引用（N-01 之前的运行、或模型照抄了占位符）：$<node_id>.result…
_PLACEHOLDER = re.compile(r"(?<!\$)\$[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*?\.result\b")


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, Mapping):
        return all(_blank(v) for v in value.values())
    if isinstance(value, list):
        return all(_blank(v) for v in value)
    return False


def deliverable_problem(value: Any) -> tuple[str, str, dict[str, Any]] | None:
    """交付本身就不能交出去的情况（docs/plan/feedback-2026-11-01.md N-02）：空的，或带着没解析的引用。
    返回（说明、代码、参数）；没问题时为 None。纯函数。"""
    if _blank(value):
        return "deliver 的结果是空的：上游的结果没有交到它，不能交付", "rule_deliverable_empty", {}
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    found = sorted(set(_PLACEHOLDER.findall(text)))
    if found:
        shown = found[:3]
        return f"交付里有没解析的引用（{'、'.join(shown)}）：上游的结果没有交到 deliver，不能交付", "rule_deliverable_unresolved", {"references": shown}
    return None


def _judging(nid: str, nodes: Mapping[str, Mapping[str, Any]], results: Mapping[str, Mapping[str, Any]]) -> bool:
    return results.get(nid, {}).get("role") in JUDGING_ROLES or nodes.get(nid, {}).get("role") in JUDGING_ROLES


def retries_within_round(result: Mapping[str, Any]) -> int:
    """Node 在本轮里已经试了几次：质量包装的轮数（Stage Runner 的 quality.rounds），或 S-08 样本记的 attempts。"""
    rounds = (result.get("quality") or {}).get("rounds") if isinstance(result.get("quality"), Mapping) else None
    return max(int(rounds or 0), int(result.get("attempts") or 0))


def unverified_nodes(results: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """succeeded 但质量包装没通过的 Node（revise 用满 max_rounds 仍没过 judge，stage_runner._revise）。"""
    return sorted(
        nid for nid, r in results.items()
        if r.get("status") == "succeeded" and isinstance(r.get("quality"), Mapping) and r["quality"].get("passed") is False
    )


def repair_targets(nodes: Mapping[str, Mapping[str, Any]], results: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """要修的 Node：失败的 Node 本身；失败的是 judge 或 gate 时，修它检查的、本轮成功的上游产出；质量包装没通过的 Node。"""
    out: set[str] = {nid for nid in unverified_nodes(results) if nid in nodes}
    for nid in sorted(n for n, r in results.items() if is_failed(r)):
        targets = [nid]
        if _judging(nid, nodes, results):
            ups = [u for u in nodes.get(nid, {}).get("after") or [] if results.get(u, {}).get("status") == "succeeded"]
            targets = ups or targets
        out.update(t for t in targets if t in nodes)
    return sorted(out)
