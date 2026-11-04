"""Learning Pipeline 的一次运行（T-16；细化见 docs/plan/learning-pipeline.md 第 2 节）。

读 trace → 按租户过滤，在平台作用域的 Telemetry 记审计日志 learning.read_traces → 归纳候选 Recipe → 观测式评测与门槛
→ 门槛通过才调 Registry：Publish（租户作用域、影子）后带评测证据 Promote。门槛不通过时不调用 Registry 的任何写方法（发布被拒）。
报告写进 report_dir（文件名同时是证据里的 report）。

结局（PipelineReport.status）：published、rejected（门槛没过）、no_candidate、dry_run、publish_failed、promote_failed。
"""

from __future__ import annotations

import contextlib
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentruntime.common.v1 import enums_pb2
from agentruntime.registry.v1 import registry_pb2 as rg
from agentruntime_core.errors import NotFoundError, ProviderError
from agentruntime_core.providers.telemetry import LogLevel, TelemetryProvider

from agentruntime_worker.activities.types import Tenant

from agentruntime_learning.pipeline.attribution import LEARNING_USER, Attribution, attribute_failures
from agentruntime_learning.pipeline.budget import LearningBudget
from agentruntime_learning.pipeline.candidates import best_route, mine_recipe, recipe_name, template_refs
from agentruntime_learning.pipeline.costs import CostSource, apply_costs
from agentruntime_learning.pipeline.evaluation import (
    DEFAULT_DELTA,
    DEFAULT_EPSILON,
    DEFAULT_MIN_SAMPLES,
    Outcomes,
    baseline_cohort,
    candidate_cohort,
    evidence,
    non_inferiority,
)
from agentruntime_learning.pipeline.traces import read_spans, task_traces

TELEMETRY_PURPOSE = "learning"
EFFECT_NAMES = {
    enums_pb2.EFFECT_NONE: "none",
    enums_pb2.EFFECT_READ: "read",
    enums_pb2.EFFECT_WRITE_REVERSIBLE: "write_reversible",
    enums_pb2.EFFECT_WRITE_IRREVERSIBLE: "write_irreversible",
}


@dataclass
class PipelineConfig:
    tenant_id: str
    task_family: str
    report_dir: Path
    min_support: int = 5
    min_samples: int = DEFAULT_MIN_SAMPLES
    delta: float = DEFAULT_DELTA
    epsilon: float = DEFAULT_EPSILON
    budget_usd: float = 0.5
    dry_run: bool = False


@dataclass
class PipelineReport:
    status: str
    tenant_id: str
    task_family: str
    reasons: list[str] = field(default_factory=list)
    candidate: str | None = None
    recipe: str | None = None
    candidate_document: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    baseline: dict[str, Any] | None = None
    gate: dict[str, Any] | None = None
    budget: dict[str, Any] = field(default_factory=dict)
    attributions: list[Attribution] = field(default_factory=list)
    tasks: int = 0
    skipped: int = 0
    other_tenant_spans: int = 0
    # 成本从哪里来：model_usage（Gateway 结算的计量表，R-06-D）或 trace（span 上的 runtime.cost_usd）；cost_unknown 是成本未知的 Task 数。
    cost_source: str = "trace"
    cost_unknown: int = 0
    path: Path | None = None

    @property
    def attribution_summary(self) -> dict[str, int]:
        return dict(sorted(Counter(a.category for a in self.attributions).items()))

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["path"] = str(self.path) if self.path else None
        data["attribution_summary"] = self.attribution_summary
        return data

    def summary(self) -> dict[str, Any]:
        """命令行最后一行输出的摘要。"""
        return {"status": self.status, "candidate": self.candidate, "recipe": self.recipe, "reasons": self.reasons, "report": str(self.path) if self.path else None}


async def _effects(registry: Any, tenant: Tenant, refs: Iterable[str]) -> dict[str, str]:
    """模板引用 → effect（按版本从 Registry 查）；查不到的不在结果里。"""
    out: dict[str, str] = {}
    for ref in refs:
        name, _, version = ref.partition("@")
        try:
            template = await registry.get_node_template(tenant, name, version)
        except NotFoundError:
            continue
        effect = EFFECT_NAMES.get(template.effect)
        if effect:
            out[ref] = effect
    return out


async def _baseline_recipes(registry: Any, tenant: Tenant, task_family: str) -> list[str]:
    """该任务族当前的默认 Recipe（ListRecipes 只列默认版本），排好序；可能有几个，现行组取它们的并集。"""
    recipes = await registry.list_recipes(tenant, task_family)
    return sorted(f"{r.name}@{r.version}" for r in recipes if r.task_family == task_family)


def _write(report: PipelineReport, directory: Path, filename: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    report.path = directory / filename
    report.path.write_text(json.dumps(report.to_json(), ensure_ascii=False, indent=2, default=str), "utf-8")


async def run_pipeline(
    config: PipelineConfig,
    *,
    lines: Iterable[str],
    fmt: str,
    registry: Any,
    gateway: Any | None,
    telemetry: TelemetryProvider,
    now: datetime | None = None,
    sources: Iterable[str] = (),
    costs: CostSource | None = None,
) -> PipelineReport:
    """sources 是读的 trace 文件，只记进审计。costs 给了时 Task 的成本读计量表（model_usage），读不了时 CostSourceError 原样抛出。"""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    # 精确到微秒：同一秒里的两次运行不互相覆盖报告。
    filename = f"{config.task_family}-{now:%Y%m%dT%H%M%S%fZ}.json"
    evaluated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    platform = telemetry.bind_platform(TELEMETRY_PURPOSE)
    budget = LearningBudget(limit_usd=config.budget_usd)
    tenant = Tenant(tenant_id=config.tenant_id, user_id=LEARNING_USER)
    report = PipelineReport(status="no_candidate", tenant_id=config.tenant_id, task_family=config.task_family)

    # 1、2：读 trace，只取这个租户的；跨租户读取留审计（S-12“跨租户读取”）。
    spans = read_spans(lines, fmt=fmt)
    traces = task_traces(spans, config.tenant_id)
    tasks = traces.tasks
    if costs is not None:
        tasks = apply_costs(tasks, await costs(config.tenant_id, [t.task_id for t in tasks]))
        report.cost_source = "model_usage"
    report.cost_unknown = sum(1 for t in tasks if not t.cost_known)
    report.tasks, report.skipped, report.other_tenant_spans = len(tasks), traces.skipped, traces.other_tenant_spans
    await platform.log(
        LogLevel.INFO,
        "learning.read_traces",
        attributes={
            "learning.tenant": config.tenant_id,
            "learning.task_family": config.task_family,
            "learning.files": ",".join(sources),
            "learning.spans": len(spans),
            "learning.tasks": len(tasks),
            "learning.skipped": traces.skipped,
            "learning.duplicates": traces.duplicates,
            "learning.other_tenant_spans": traces.other_tenant_spans,
            "learning.cost_source": report.cost_source,
            "learning.cost_unknown": report.cost_unknown,
        },
    )

    try:
        # 3：候选 Recipe。
        route = best_route(tasks, config.task_family, min_support=config.min_support)
        if route is None:
            report.reasons.append(f"没有成功次数不少于 {config.min_support} 的做法")
            return report
        shapes = [r.shape for r in route[1][-1].rounds if r.planned and r.shape is not None]
        templates = await _effects(registry, tenant, template_refs(shapes))
        candidate = mine_recipe(tasks, config.task_family, templates=templates, min_support=config.min_support)
        if candidate is None:
            report.reasons.append("候选 Recipe 不合格（模板查不到 effect，或正文不合 recipe.schema.json）")
            return report
        report.candidate = candidate.name
        report.candidate_document = candidate.document

        # 4：评测与门槛。
        baseline_refs = await _baseline_recipes(registry, tenant, config.task_family)
        cand_tasks = candidate_cohort(tasks, config.task_family, candidate.signature)
        base_tasks = baseline_cohort(tasks, config.task_family, baseline_refs)
        report.baseline = (
            {"kind": "recipe", "recipes": baseline_refs, "samples": len(base_tasks)} if baseline_refs else {"kind": "full_planning", "samples": len(base_tasks)}
        )
        gate = non_inferiority(
            Outcomes.from_tasks(cand_tasks), Outcomes.from_tasks(base_tasks), delta=config.delta, epsilon=config.epsilon, min_samples=config.min_samples
        )
        report.gate = {"passed": gate.passed, "details": gate.details, "min_samples": gate.min_samples}
        report.evidence = evidence(
            gate, task_family=config.task_family, task_ids=[t.task_id for t in [*cand_tasks, *base_tasks]], report=filename, evaluated_at=evaluated_at
        )

        # 失败归因（J.2），单独的预算。
        evaluated = {t.task_id: t for t in [*cand_tasks, *base_tasks]}
        report.attributions = await attribute_failures(
            evaluated.values(), tenant_id=config.tenant_id, task_family=config.task_family, gateway=gateway, budget=budget
        )

        # 5：发布。门槛不通过时不调用 Registry 的任何写方法。
        if not gate.passed:
            report.status = "rejected"
            report.reasons = ["发布被拒：没过评测门槛", *gate.reasons]
            return report
        if config.dry_run:
            report.status = "dry_run"
            return report
        name = recipe_name(config.task_family)
        try:
            published = await registry.publish(
                tenant,
                rg.REGISTRY_KIND_RECIPE,
                name,
                candidate.document,
                change_note=f"Learning Pipeline：从 {len(tasks)} 条 trace 归纳（支持 {candidate.support} 条），评测 {report.evidence['eval_set']['version']}",
            )
        except ProviderError as err:
            report.status = "publish_failed"
            report.reasons.append(f"Publish 失败：{err.code}：{err.message}")
            return report
        try:
            await registry.promote(tenant, rg.REGISTRY_KIND_RECIPE, name, published.version, report.evidence)
        except ProviderError as err:
            report.status = "promote_failed"
            report.recipe = None
            report.reasons.append(f"Promote 被拒：{err.code}：{err.message}；影子版本 {name}@{published.version} 留在 Registry 里，默认版本不变")
            return report
        report.status = "published"
        report.recipe = f"{name}@{published.version}"
        return report
    finally:
        report.budget = budget.report()
        await platform.observe("learning.cost_usd", budget.spent_usd, attributes={"learning.task_family": config.task_family, "learning.calls": budget.calls})
        with contextlib.suppress(ProviderError):
            await platform.flush()
        _write(report, config.report_dir, filename)


def summary_line(report: PipelineReport) -> str:
    return json.dumps(report.summary(), ensure_ascii=False)
