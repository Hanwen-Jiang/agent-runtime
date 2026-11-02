"""Reviewer 的 decision activity（计划 S-10）：review_round 与 finalize_task。AR_REVIEWER=model 时由它们替换桩。"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError


from agentruntime_worker import names
from agentruntime_worker.executor.dev import model_gateway
from agentruntime_worker.activities.types import FinalizeTaskInput, ReviewRoundInput, ReviewRoundOutput, StageResult, Tenant
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.executor.dev import data_root, dev_object_store
from agentruntime_worker.executor.heartbeat import heartbeating
from agentruntime_worker.reviewer.reviewer import Reviewer, ReviewRequest
from agentruntime_worker.reviewer.rules import ReviewCatalog
from agentruntime_worker.memory import memory_client
from agentruntime_worker.reviewer.wrap_up import WrapUp
from agentruntime_worker.telemetry import worker_telemetry
from agentruntime_worker.workflows import stage_plan as sp

logger = logging.getLogger(__name__)


class CatalogSource(Protocol):
    async def load(self, tenant: Tenant, task_id: str) -> ReviewCatalog: ...


class StaticCatalogSource:
    """开发与测试用：每个租户一份固定的 Catalog；没登记的租户用 default。"""

    def __init__(self, default: ReviewCatalog, by_tenant: dict[str, ReviewCatalog] | None = None) -> None:
        self.default = default
        self.by_tenant = dict(by_tenant or {})

    async def load(self, tenant: Tenant, task_id: str) -> ReviewCatalog:
        return self.by_tenant.get(tenant.tenant_id, self.default)


def _lower_ceiling(a: str, b: str | None) -> str:
    if not b or b not in sp.EFFECT_ORDER:
        return a
    return min(a, b, key=sp.effect_rank)


def _result_dict(result: Any) -> dict[str, Any]:
    return dataclasses.asdict(result) if isinstance(result, StageResult) else dict(result)


class ReviewerActivities:
    def __init__(self, reviewer: Reviewer, catalogs: CatalogSource, wrap_up: WrapUp | None) -> None:
        self.reviewer = reviewer
        self.catalogs = catalogs
        self.wrap_up = wrap_up

    async def review(self, inp: ReviewRoundInput) -> ReviewRoundOutput:
        """review_round 的本体（测试的脚本也直接调用它）。"""
        if inp.stage is None:
            raise ApplicationError("review_round 缺少本轮执行的 Stage：工作流早于 S-10，不能交给真正的 Reviewer", type="invalid_review_input", non_retryable=True)
        catalog = await self.catalogs.load(inp.tenant, inp.task_id)
        ceiling = _lower_ceiling(catalog.effect_ceiling, (inp.task_state.get("constraints") or {}).get("effect_ceiling"))
        result = await self.reviewer.review(
            ReviewRequest(
                tenant=inp.tenant,
                task_id=inp.task_id,
                round_seq=inp.round_seq,
                task_state=inp.task_state,
                stage=inp.stage,
                results={nid: _result_dict(r) for nid, r in inp.stage_results.items()},
                history=list(inp.history),
                catalog=ReviewCatalog(recipes=dict(catalog.recipes), effect_ceiling=ceiling),
                rules_only=inp.rules_only,
                verification=inp.verification,
            )
        )
        # 判定依据与模型调用写入日志（trace），不传给下游。
        logger.info(
            "Reviewer：第 %s 轮判 %s%s；依据 %s；模型调用 %s%s%s",
            inp.round_seq,
            result.verdict["verdict"],
            "（代码按规则上调）" if result.raised else "",
            "；".join(result.reasons) or "无",
            [(c.purpose, c.tier, c.vendor) for c in result.calls],
            "；Progress Check 不可用，只按规则判定" if result.fallback else "",
            (f"；Jev（{result.checks.model}，输入 {result.checks.input_tokens} token）：回答了 {result.checks.answers:.2f}、不认工具或输入 {result.checks.disowns:.2f}"
             if result.checks is not None else ""),
        )
        return ReviewRoundOutput(verdict=result.verdict, state_update=result.state_update, cost_usd=round(sum(c.cost_usd for c in result.calls), 6), highlights=result.highlights)

    @activity.defn(name=names.REVIEW_ROUND)
    async def review_round(self, inp: ReviewRoundInput) -> ReviewRoundOutput:
        async with heartbeating():
            return await self.review(inp)

    @activity.defn(name=names.FINALIZE_TASK)
    async def finalize_task(self, inp: FinalizeTaskInput) -> None:
        if self.wrap_up is None:
            logger.info("没有配置收尾写回，跳过：%s", inp.task_id)
            return
        report = await self.wrap_up.finalize(inp)
        logger.info("收尾写回：%s 归档到 %s，提交 %d 条候选", inp.task_id, report.archive_uri, len(report.candidates))

    def activities(self) -> dict[str, Any]:
        return {names.REVIEW_ROUND: self.review_round, names.FINALIZE_TASK: self.finalize_task}


def dev_reviewer(config: WorkerConfig) -> ReviewerActivities:
    """AR_REVIEWER=model 时的开发组装：真正的 Reviewer 加桩模型、空的 Recipe 列表与开发实现的写回。

    Model Gateway 接好之前，桩模型答不出合格的 Progress Check，Reviewer 只按规则判定；没设 AR_MEMORY_ADDRESS 时
    经验候选只记在进程内。真模型上的跑分见 py-learning 的评测（S-08、S-10）。
    """
    root = data_root(config)
    wrap_up = WrapUp(objects=dev_object_store(config, root), memory=memory_client(config), telemetry=worker_telemetry())
    judge = None
    if config.judge == "jev":
        # 交付的快速检查（docs/plan/feedback-2026-11-01.md N-09）：密钥在 SecretStore 平台作用域的 judge/jev-api-key。
        from agentruntime_worker.reviewer.judge import JevJudge, file_key, secret_key

        key = file_key(config.judge_key_file) if config.judge_key_file is not None else secret_key(config.store_paths(root)[1])
        judge = JevJudge(key, url=config.judge_url, model=config.judge_model)
    # 这份 Catalog 不另设上限：上限是 Task 自己的（constraints.effect_ceiling，review 里取两者较低的）。原来写的 read 让每个 Task 的 Reviewer
    # 都以为只能读（TMP 计划第 11.15 节）。
    return ReviewerActivities(Reviewer(model_gateway(config), judge=judge), StaticCatalogSource(ReviewCatalog(effect_ceiling=sp.EFFECT_ORDER[-1])), wrap_up)
