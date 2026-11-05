"""Reviewer（计划 S-10）：硬信号 → 一次 Progress Check → 规则兜底 → Verdict 与汇总。

- 代码先算台账与硬信号（signals），再算 floor（rules）；Progress Check 只调用一次模型（强档，独立上下文，
  材料由 prompts.review_view 按白名单取出），不合格时带上问题重问一次，仍不合格或模型出错就只按规则判定。
- Triage 为 fast 的 Round 不调用模型（设计书 3.2.1 快速路径）：只按规则做格式检查。
- 模型的提议交给 rules.enforce：细节改成合法的，低于 floor 的抬上去，用满上限的升一级。
- 汇总：模型提议的事实与假设经检查、截断；产物由代码从结果里取（只收本租户的对象）；replan 卡住的原因写进事实。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import enums_pb2, payload_pb2, tenant_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.executor.model_gateway import ModelGateway, billed_cost_usd
from agentruntime_worker.planner.planner import ModelCall
from agentruntime_worker.reviewer import prompts
from agentruntime_worker.reviewer.judge import DeliverableJudge, JudgeChecks, reply_text
from agentruntime_worker.reviewer.rules import Caps, Floor, ReviewCatalog, Thresholds, deliverable_problem, enforce, floor
from agentruntime_worker.reviewer.signals import is_failed, ledger
from agentruntime_worker.reviewer.summary import artifacts_in, checked_update


@dataclass(frozen=True)
class ReviewerConfig:
    tier: str = "strong@medium"
    """Progress Check 用的 Tier。"""
    escalate_tier: str = "strong@high"
    """代码把判定抬到“换更强的 Tier”时用的 Tier。"""
    caps: Caps = Caps()
    thresholds: Thresholds = Thresholds()
    max_state_items: int = 10
    """汇总里每一项最多写进 Task State 的条数。"""


@dataclass(frozen=True)
class ReviewRequest:
    tenant: Tenant
    task_id: str
    round_seq: int
    task_state: dict[str, Any]
    stage: dict[str, Any]
    results: dict[str, dict[str, Any]]
    """Node id → 结果（StageResult 的字段；S-08 的样本另有 attempts）。"""
    history: list[dict[str, Any]]
    catalog: ReviewCatalog | None = None
    # 决策调用的重试用完或到了总时限（TMP 计划第 4 节）：不调模型，只按规则判定。
    rules_only: bool = False
    # 这个 Task 改过文件、验证过没有（Stage Runner 记的，TMP 计划第 11.15 节）；评测样本与之前的运行没有。
    verification: dict[str, Any] | None = None


@dataclass
class ReviewResult:
    verdict: dict[str, Any]
    state_update: dict[str, Any]
    completion: float | None
    signals: tuple[str, ...]
    floor: Floor
    raised: bool
    """代码把模型的提议往上抬了。"""
    fallback: bool
    """Progress Check 两次都不合格，或模型出错，判定只来自规则。"""
    calls: list[ModelCall] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    """判定依据（写入 trace，不传给下游）。"""
    highlights: dict[str, Any] | None = None
    """这一轮的重要输出（同一次 Progress Check 给的）；不合格时没有，不为它重问。"""
    checks: JudgeChecks | None = None
    """Jev 对要交付的结果的检查（N-09）；没配、不是要判 done、或调用失败时为 None。写入 trace。"""


def generator_vendor(results: Mapping[str, Mapping[str, Any]]) -> str:
    """生成本轮结果的供应商里最多的那一家（并列取名字靠前的）；没有模型生成的结果时为空。Reviewer 调用时避开它（附录 H.3）。"""
    counts = Counter(str(r.get("vendor") or "") for r in results.values() if isinstance(r, Mapping) and r.get("vendor"))
    if not counts:
        return ""
    top = max(counts.values())
    return min(v for v, n in counts.items() if n == top)


def _error_code(exc: BaseException) -> str:
    """模型调用失败的代码：网关的错误代码（取得到时）或异常的类型名，snake_case，最多 64 个字符。"""
    code = str(getattr(exc, "code", "") or "") if isinstance(getattr(exc, "code", None), str) else ""
    raw = code or type(exc).__name__
    snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\g<1>", raw).lower()
    snake = re.sub(r"[^a-z0-9_]+", "_", snake).strip("_")[:64]
    return snake or "unknown"


def _model_failure_cause(exc: BaseException) -> dict[str, Any]:
    """Keep timeout fallbacks distinguishable from other model failures.

    The reason is written into the existing rule-fallback parameters so callers
    can diagnose slow/unavailable gateways without adding a new event or state
    field. Gateway-specific deadline codes are treated like Python timeouts.
    """
    error = _error_code(exc)
    if isinstance(exc, TimeoutError) or any(token in error for token in ("timeout", "deadline")):
        return {"cause": "model_call_timeout", "error": error}
    return {"cause": "model_call_failed", "error": error}


def _value(data: Any) -> struct_pb2.Value:
    value = struct_pb2.Value()
    json_format.ParseDict(data, value)
    return value


class Reviewer:
    def __init__(self, gateway: ModelGateway, config: ReviewerConfig | None = None, *, judge: DeliverableJudge | None = None) -> None:
        self.gateway = gateway
        self.config = config or ReviewerConfig()
        # 交付的快速检查（docs/plan/feedback-2026-11-01.md N-09）；没配时为 None。
        self.judge = judge

    async def review(self, request: ReviewRequest) -> ReviewResult:
        cfg = self.config
        results = {nid: dict(r) for nid, r in request.results.items()}
        lg = ledger(request.task_state, request.stage, results, request.history)
        rounds = request.task_state.get("rounds") or []
        fast = bool(rounds) and rounds[-1].get("triage_level") == "fast"
        calls: list[ModelCall] = []
        problems: list[str] = []
        answer: dict[str, Any] | None = None
        # 没有模型判定时的原因（docs/plan/feedback-2026-11-01.md N-02）：只有代码与错误类型，不含模型的原文。
        cause: dict[str, Any] = {"cause": "fast_reply"} if fast else {"cause": "decision_retries_exhausted"} if request.rules_only else {}
        if not fast and not request.rules_only:
            preview = floor(lg, results, completion=None, thresholds=cfg.thresholds)
            view = prompts.review_view(request.task_state, request.stage, results, request.history)
            messages = prompts.review_messages(view, ledger=lg, floor=preview, catalog=request.catalog, verification=request.verification)
            for attempt in range(2):
                try:
                    response = await self._call(request, messages, calls)
                except Exception as exc:  # noqa: BLE001 — 模型出错算一次不合格，最后按规则判定（取消不是 Exception，原样往外走）
                    problems = [f"调用模型失败：{type(exc).__name__}"]
                    cause = _model_failure_cause(exc)
                    text = ""
                else:
                    value = json_format.MessageToDict(response.result) if response.HasField("result") else None
                    text = response.text if value is None else json.dumps(value, ensure_ascii=False)
                    value = prompts.normalize_answer(value)
                    problems = prompts.answer_problems(value)
                    cause = {"cause": "invalid_answer", "problems": len(problems)} if problems else {}
                    if not problems:
                        assert isinstance(value, dict)
                        answer = value
                        break
                if attempt == 0:
                    if lg.budget_usd and lg.spent_usd + sum(c.cost_usd for c in calls) >= lg.budget_usd:
                        # 美元预算已经用完：不再重问，按规则判定（TMP 计划第 11.11 节；第一次评审照常，按 11.4 不拦）。
                        problems = [*problems, "美元预算已经用完，不再重问"]
                        cause = {**cause, "budget_spent": True}
                        break
                    messages = prompts.retry_messages(messages, text, problems)
        completion = float(answer["completion"]) if answer is not None and answer.get("completion") is not None else None
        fl = floor(lg, results, completion=completion, thresholds=cfg.thresholds)
        checks = await self._judged(request, results, answer)
        decision = enforce(
            answer["verdict"] if answer is not None else None,
            floor=fl,
            ledger=lg,
            stage=request.stage,
            results=results,
            catalog=request.catalog,
            caps=cfg.caps,
            escalate_tier=cfg.escalate_tier,
            verification=request.verification,
            checks=checks,
        )
        verdict = decision.verdict
        if answer is None and verdict.get("reason_code") == "rule_fallback" and cause:
            verdict["reason_params"] = {**(verdict.get("reason_params") or {}), **cause}
        update: dict[str, Any] = checked_update(answer.get("state_update") if answer is not None else None, max_items=cfg.max_state_items)
        stuck = (verdict.get("replan") or {}).get("stuck_reason") if verdict["verdict"] == "replan" else None
        if stuck:
            facts = update.setdefault("facts", [])
            entry = f"卡住的原因：{stuck}"
            if entry not in facts:
                facts[cfg.max_state_items - 1 :] = [entry]
        artifacts = artifacts_in(results, request.tenant.tenant_id)
        if artifacts:
            update["artifacts"] = artifacts
        return ReviewResult(
            verdict=verdict,
            state_update=update,
            completion=completion,
            signals=tuple(verdict.get("signals") or ()),
            floor=fl,
            raised=decision.raised,
            fallback=not fast and not request.rules_only and answer is None,
            calls=calls,
            problems=problems if answer is None else [],
            reasons=[*fl.reasons, *decision.reasons],
            highlights=prompts.checked_highlights(answer.get("highlights")) if answer is not None else None,
            checks=checks,
        )

    async def _judged(self, request: ReviewRequest, results: Mapping[str, Mapping[str, Any]], answer: Mapping[str, Any] | None) -> JudgeChecks | None:
        """要判 done 时请 Jev 看一眼要交付的结果（N-09）：模型提议 done，或没有模型的提议、规则会判 done（没有失败的 Node、有成功的
        deliver）。交付本身已经是空的或带占位符的不问（规则已经会拦）。"""
        if self.judge is None:
            return None
        proposal = answer.get("verdict") if answer is not None else None
        if isinstance(proposal, Mapping) and proposal.get("verdict") != "done":
            return None
        if proposal is None and any(is_failed(r) for r in results.values()):
            return None
        roles = {str(n.get("id")): n.get("role") for n in request.stage.get("nodes") or []}
        delivers = sorted(nid for nid, r in results.items() if r.get("status") == "succeeded" and "deliver" in (r.get("role"), roles.get(nid)))
        if not delivers:
            return None
        wanted = str(((proposal or {}).get("done") or {}).get("deliver_node") or "")
        node = wanted if wanted in delivers else delivers[0]
        value = results[node].get("result")
        if deliverable_problem(value) is not None:
            return None
        return await self.judge.check(str(request.task_state.get("goal") or ""), reply_text(value))

    async def _call(self, request: ReviewRequest, messages: list[mg.ChatMessage], calls: list[ModelCall]) -> mg.GenerateResponse:
        t = request.tenant
        req = mg.GenerateRequest(
            tenant=tenant_pb2.TenantContext(tenant_id=t.tenant_id, user_id=t.user_id, workspace_id=t.workspace_id, session_id=t.session_id),
            call=mg.CallContext(
                task_id=request.task_id, node_id="review", role=enums_pb2.ROLE_JUDGE, tier=self.config.tier, avoid_vendor=generator_vendor(request.results)
            ),
            generation=mg.GenerateParams(messages=messages, output_schema=payload_pb2.JsonPayload(value=_value(prompts.REVIEW_OUTPUT_SCHEMA))),
        )
        try:
            response = await self.gateway.generate(req)
        except Exception as exc:
            # 失败之前已经计费的也算进成本（TMP 计划第 11.11 节）。
            if billed := billed_cost_usd(exc):
                calls.append(ModelCall(purpose="review", tier=self.config.tier, vendor="", cost_usd=billed))
            raise
        calls.append(ModelCall(purpose="review", tier=response.model.tier or self.config.tier, vendor=response.model.vendor, cost_usd=response.usage.cost_usd_micros / 1_000_000))
        return response
