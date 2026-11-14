"""Public, source-free projection of evidence-summary optimization experiments.

Fresh paired validation is distinct from tuning cases and historical model-only
LongBench results. No prompt, source passage, prediction or ledger is projected.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any

SCHEMA = "longbench-context-optimization-v1"
PROTOCOL = "longbench-context-evidence-v1"
RETRIEVAL_PROTOCOL = "longbench-context-retrieval-v2"
FOCUS_PROTOCOL = "longbench-context-source-coverage-v3"
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
REVISION = re.compile(r"^[0-9a-f]{7,40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ARMS = ("baseline", "candidate")
METRICS = (
    ("answered_count", "返回答案题数", "Items with answers", "number"),
    ("answer_format_failure_count", "答案格式错误题数", "Answer-format failures", "number"),
    ("infrastructure_error_count", "无有效结果／待补跑题数", "Items without valid results / awaiting rerun", "number"),
    ("input_tokens", "全部尝试已结算输入 token", "Settled input tokens across all attempts", "number"),
    ("answer_input_tokens", "所选有效尝试的答题输入 token", "Answer input tokens of selected valid attempts", "number"),
    ("helper_input_tokens", "所选有效尝试的摘要与检查输入 token", "Summary/check input tokens of selected valid attempts", "number"),
    ("output_tokens", "全部尝试已结算输出 token", "Settled output tokens across all attempts", "number"),
    ("total_tokens", "全部尝试已结算 token 合计", "Settled total tokens across all attempts", "number"),
    ("context_tokens_mean", "答题上下文平均估算 token", "Mean estimated answer-context tokens", "number"),
    ("elapsed_seconds", "所选有效尝试耗时", "Elapsed time of selected valid attempts", "seconds"),
    ("cost_usd", "全部尝试已结算调用费用", "Settled call cost across all attempts", "usd"),
    ("summary_calls", "摘要／证据提取调用次数", "Summarization / evidence-extraction calls", "number"),
    ("evidence_accepted_quotes", "通过原文校验的证据引文数", "Source-verified evidence quotes", "number"),
    ("evidence_rejected_quotes", "未通过原文校验的引文数", "Rejected evidence quotes", "number"),
    ("fallback_batches", "保留原文的失败批次数", "Failed batches retained verbatim", "number"),
    ("probe_count", "任务元数据检查次数", "Task-metadata fact checks", "number"),
    ("probe_failures", "任务元数据检查未通过次数", "Failed task-metadata checks", "number"),
)


def _text(value: Any, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError("invalid optimization text")
    return value


def _number(value: Any, *, maximum: float = 2**53 - 1) -> int | float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError("invalid optimization metric")
    return value


def _count(value: Any, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("invalid optimization count")
    return value


def _flag(value: Any) -> bool | None:
    if value is not None and type(value) is not bool:
        raise ValueError("invalid optimization flag")
    return value


def _timestamp(value: Any) -> str:
    text = _text(value, 64)
    if datetime.fromisoformat(text.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("optimization timestamp requires timezone")
    return text


def project_context_optimization(raw: bytes) -> dict[str, Any]:
    report = json.loads(raw)
    if (not isinstance(report, dict) or report.get("schema") != SCHEMA or report.get("protocol") not in {PROTOCOL, RETRIEVAL_PROTOCOL, FOCUS_PROTOCOL}
            or report.get("evaluation_type") != "ablation" or report.get("evaluation_scope") != "component"):
        raise ValueError("unsupported context optimization protocol")
    protocol = report["protocol"]
    retrieval = protocol == RETRIEVAL_PROTOCOL
    focus = protocol == FOCUS_PROTOCOL
    identity = _text(report.get("id"), 128)
    if not identity.startswith("longbench-context-") or not IDENTIFIER.fullmatch(identity) or report.get("comparison_id") != identity:
        raise ValueError("invalid optimization identity")
    split = report.get("split")
    if split not in {"development", "validation"}:
        raise ValueError("optimization requires explicit development/validation split")
    answer_protocol = report.get("answer_protocol")
    expected_answer_protocol = "json-choice-v1-stream-identical-both-arms" if focus else "json-choice-v1-identical-both-arms"
    if answer_protocol != expected_answer_protocol:
        raise ValueError("both optimization arms must use the same answer protocol")
    count = _count(report.get("sample_count"), 10000)
    if count == 0:
        raise ValueError("optimization cohort must be nonempty")
    completed = _count(report.get("completed_count"), count)
    repeats = _count(report.get("repeats"), 100)
    if repeats == 0:
        raise ValueError("optimization repeat count is missing")
    complete = _flag(report.get("collection_complete"))
    status = report.get("status")
    if complete is None or status not in {"running", "blocked", "completed"} or (status == "completed") != complete or complete and completed != count:
        raise ValueError("inconsistent optimization completion")
    stamp = _timestamp(report.get("completed_at") if complete else report.get("updated_at"))
    revision = _text(report.get("runtime_revision"), 40)
    if not REVISION.fullmatch(revision):
        raise ValueError("optimization Runtime revision is missing")
    hashes = {key: _text(report.get(key), 64) for key in ("runtime_source_sha256", "config_sha256", "cohort_sha256")}
    if any(not SHA256.fullmatch(value) for value in hashes.values()):
        raise ValueError("invalid optimization provenance")
    dataset = _text(report.get("dataset_name"), 128)
    if dataset != "LongBench v2":
        raise ValueError("unexpected optimization dataset")
    version, model = _text(report.get("dataset_version"), 128), _text(report.get("model_profile"), 256)
    arms = report.get("arms")
    if not isinstance(arms, dict) or set(arms) != set(ARMS):
        raise ValueError("optimization requires two fresh arms")
    denominator = completed * repeats
    run_ids, correct, invalid = {}, {}, {}
    for name in ARMS:
        arm = arms[name]
        if not isinstance(arm, dict):
            raise ValueError("invalid optimization arm")
        run = _text(arm.get("run_id"), 128)
        if not IDENTIFIER.fullmatch(run):
            raise ValueError("invalid optimization run id")
        run_ids[name] = run
        # Counts must represent exactly the completed paired cohort. Unpaired
        # executions may contribute cost/usage but never quality numerators.
        correct[name] = _count(arm.get("correct_count"), denominator)
        invalid[name] = _count(arm.get("infrastructure_error_count", 0), denominator)
        if correct[name] + invalid[name] > denominator:
            raise ValueError("correct and unavailable outcomes overlap")
        if arm.get("paired_sample_count") is not None and arm["paired_sample_count"] != denominator:
            raise ValueError("arm quality denominator differs from completed pairs")
    if len(set(run_ids.values())) != 2:
        raise ValueError("optimization arm run IDs must differ")
    quality = _flag(report.get("quality_valid")) is True and complete and not any(invalid.values())
    # A development score may be observed, but does not establish held-out
    # improvement; never project statistical improvement fields for it.
    validation_frozen = _flag(report.get("validation_cohort_frozen_before_tuning")) is True
    if split == "validation" and not validation_frozen:
        quality = False
    publish_comparison = quality and split == "validation" and validation_frozen
    metrics = []
    def metric(key: str, zh: str, en: str, unit: str, values: list[Any]) -> None:
        if any(v is not None for v in values):
            metrics.append({"key": key, "label_zh": zh, "label_en": en, "unit": unit, "baseline": values[0], "candidate": values[1]})
    metric("correct_count", "已判定答对题数", "Confirmed correct answers", "number", [correct[a] for a in ARMS])
    if quality and denominator:
        metric("accuracy", "独立验收集答题准确率" if split == "validation" else "开发集答题准确率（诊断）",
               "Validation answer accuracy" if split == "validation" else "Development accuracy (diagnostic)", "rate", [correct[a] / denominator for a in ARMS])
    usage_unknown = {name: invalid[name] > 0 or arms[name].get("usage_known") is False or arms[name].get("elapsed_known") is False for name in ARMS}
    pending = _number(report.get("pending_reserved_usd"))
    # Ledger totals include every attempt, including discarded/failed attempts.
    # Selected-attempt breakdowns and duration have a different scope. Pending
    # old billing must not invalidate a later complete, pinned answer pair.
    ledger_usage_unknown = bool(pending) or _flag(report.get("cost_known")) is not True
    for key, zh, en, unit in METRICS:
        values = [_number(arms[a].get(key)) for a in ARMS]
        ledger_metric = key in {"input_tokens", "output_tokens", "total_tokens", "cost_usd"}
        selected_metric = key in {"answer_input_tokens", "helper_input_tokens", "elapsed_seconds"}
        if ledger_metric and ledger_usage_unknown or selected_metric and any(usage_unknown.values()):
            zh += "（下界）"; en += " (lower bound)"
        if any(usage_unknown.values()) and (ledger_metric or selected_metric):
            values = [None if usage_unknown[a] and v == 0 else v for a, v in zip(ARMS, values)]
        metric(key, zh, en, unit, values)
    paired = report.get("paired") if publish_comparison else None
    comparison = {}
    if paired is not None:
        if not isinstance(paired, dict):
            raise ValueError("invalid optimization paired statistics")
        gained, lost = _count(paired.get("gained"), denominator), _count(paired.get("lost"), denominator)
        if gained + lost > denominator or correct['candidate'] - correct['baseline'] != gained - lost:
            raise ValueError("paired outcome counts do not reconcile")
        delta = paired.get("accuracy_delta")
        if type(delta) not in (int, float) or not math.isfinite(delta) or not math.isclose(delta, (gained-lost)/denominator, abs_tol=1e-9):
            raise ValueError("optimization accuracy difference does not reconcile")
        interval = paired.get("accuracy_delta_ci95")
        if not isinstance(interval, list) or len(interval) != 2 or any(type(v) not in (int,float) or not math.isfinite(v) or not -1<=v<=1 for v in interval) or interval[0]>interval[1]:
            raise ValueError("invalid optimization confidence interval")
        comparison = {"paired_gained": gained, "paired_lost": lost, "paired_accuracy_delta": delta,
                      "paired_ci95_low": interval[0], "paired_ci95_high": interval[1],
                      "paired_mcnemar_p": _number(paired.get("mcnemar_exact_two_sided_p"), maximum=1)}
    pending, actual, known = (_number(report.get(k)) for k in ("pending_reserved_usd", "actual_cost_usd", "known_cost_usd"))
    cost_known = _flag(report.get("cost_known")) is True and complete and not pending
    if cost_known and actual is None:
        raise ValueError("optimization settled total is missing")
    split_zh = "独立验收" if split == "validation" else "开发诊断"
    split_en = "Independent validation" if split == "validation" else "Development diagnostics"
    limitations_zh = [
        f"{split_zh}集：固定 {count} 道题，两臂分别使用原完整压缩策略与分批原文证据摘要；不是“仅省略工具输出”基线。",
        "两臂使用同一模型、原文、题目与结构化选项输出；没有向摘要、证据提取或答题提示提供标准答案。",
        "每批约16000估算token只是处理粒度，不是输入拒绝上限；所有原文片段均被处理，证据引文必须能在对应来源中核对。",
        "这是Runtime上下文组件优化对照，不是官方纯模型LongBench榜单成绩，也不是完整Task API端到端测评。",
        "调用异常不计为答错。只有最终所选的配对回答都有效时才展示准确率；历史失败尝试的费用待结算，不会把后来取得的有效回答判错，也不单独否定其质量评分。",
        "输入、输出及合计 token 来自全部尝试的已结算用量记录，包含失败与重试；仍有未结算调用时只是已知下界。",
        "答题输入、摘要与检查输入以及耗时仅统计所选有效尝试，不是全部尝试的总用量或实验墙钟时间；它们不能与全部尝试的用量记录合计直接相加核对。",
        "全部失败及重试的已知费用均计入用量记录；未结算预占另列，不是已结算费用，也不代表调用免费。",
    ]
    limitations_en = [
        f"{split_en}: {count} fixed questions compare the original full compression strategy with batched source-evidence summaries; this is not a mask-only baseline.",
        "Both arms use the same model, source documents, questions and structured-choice output. Gold answers never enter summary, extraction or answering prompts.",
        "The roughly 16000-token batch target is processing granularity, not an input-rejection ceiling. All source parts are processed and extracted quotes are checked against their sources.",
        "This is a Runtime context-component comparison, not an official model-only LongBench score or an end-to-end Task API evaluation.",
        "Infrastructure failures are not wrong answers. Accuracy is shown only when the selected final pairs are valid; unsettled billing for historical failed attempts does not turn a later valid answer into an error or by itself invalidate quality scores.",
        "Input, output and total tokens come from settled ledger entries across all attempts, including failed attempts and retries. They are lower bounds while usage remains unsettled.",
        "Answer input, summary/check input and elapsed time describe only selected valid attempts, not all-attempt usage or experiment wall-clock duration. They do not directly reconcile with all-attempt ledger totals.",
        "Known charges from all failed attempts and retries remain in the ledger; pending reservations are shown separately, are not settled costs and do not imply free calls.",
    ]
    if retrieval:
        limitations_zh[0] = f"{split_zh}集：固定 {count} 道题，两臂分别使用上一版分批证据摘要与原文检索及证据核对；不是“仅省略工具输出”基线。"
        limitations_en[0] = f"{split_en}: {count} fixed questions compare the previous batched-evidence summary with source retrieval and evidence checking; this is not a mask-only baseline."
        limitations_zh[2] = "候选以问题和各选项分别进行 BM25 原文窗口检索，补充相邻上下文并保持来源顺序；选项只是待核对的命题，不作为事实。返回片段保持原文，可按来源位置核对。"
        limitations_en[2] = "The candidate runs separate BM25 window queries for the question and each option, adds neighboring context and preserves source order. Options are hypotheses, not facts; passages remain verbatim and traceable to their source positions."
        limitations_zh.insert(3, "全局覆盖、计数、极值或时间变化问题，以及检索信号不足时，候选回退完整原文。窗口选择不调用模型生成摘要，不另设实验输入拒绝或费用硬上限；模型自身窗口仍适用。")
        limitations_en.insert(3, "The candidate retains the full source for global coverage, counting, extrema, temporal-change questions or weak retrieval signals. Selection does not generate model summaries or add experimental input-rejection or spending caps; the model's own window still applies.")
    if focus:
        limitations_zh[0] = f"{split_zh}集：固定 {count} 道题，对比原文检索 v2 与保留相同检索并增加原文证据聚焦的 v3；不是“仅省略工具输出”基线。"
        limitations_en[0] = f"{split_en}: {count} fixed questions compare source retrieval v2 with v3, which keeps that retrieval and adds source-evidence focus; this is not a mask-only baseline."
        limitations_zh[2] = "两臂保留相同原文检索与全文回退；v3 分批读取全部原文，提取候选断言的支持或反证，引文经逐字校验后追加到答题上下文，不输出选项判决。批次是处理粒度，不是输入拒绝上限。"
        limitations_en[2] = "Both arms retain identical retrieval and full-source fallbacks. V3 processes every source batch for supporting or contradicting evidence, verifies verbatim quotes, and appends them without option verdicts. Batches are processing units, not input-rejection caps."
    if split == "development":
        limitations_zh.insert(0, "开发集已用于错误分析与策略设计，只作诊断，不作为未见数据上的提升证据。")
        limitations_en.insert(0, "Development cases were used for error analysis and design; scores are diagnostic, not evidence of improvement on unseen data.")
    elif validation_frozen:
        limitations_zh.insert(0, "验收题集在调试前固定，排除此前80道开发及验收题和3道预览题；策略冻结后才运行该集。" if focus else "验收题集在调试前固定，并与此前40道开发题及上一轮20道验收题不重叠；策略冻结后才运行该集。" if retrieval else "验收题集在调试前固定，并与此前40道开发题不重叠；策略冻结后才运行该集。")
        limitations_en.insert(0, "Validation questions were frozen before tuning and exclude all 80 prior development/validation questions plus three previewed questions; evaluation follows strategy freeze." if focus else "Validation questions were frozen before tuning and are disjoint from the prior 40 development cases and 20 earlier validation cases; evaluation follows strategy freeze." if retrieval else "Validation questions were frozen before tuning and are disjoint from the prior 40 development cases; evaluation follows strategy freeze.")
    else:
        limitations_zh.insert(0, "验收题集预先冻结状态未确认，不发布配对提升或置信区间。")
        limitations_en.insert(0, "Pre-tuning validation freeze is unconfirmed; paired improvement and intervals are not published.")
    return {"id": identity, "kind": "context-ablation" if split == "validation" else "context-diagnostics", "evaluation_type": "ablation", "evaluation_scope": "component",
            "title_zh": "LongBench v2 · " + ("原文证据聚焦 v3" if focus else "原文检索优化" if retrieval else "证据摘要优化") + " · " + split_zh,
            "title_en": "LongBench v2 · " + ("Source-evidence focus v3" if focus else "Source-retrieval optimization" if retrieval else "Evidence-summary optimization") + " · " + split_en,
            "comparison_id": identity, "runtime_revision": revision, **hashes, "protocol": protocol,
            "dataset_name": dataset, "dataset_version": version, "model_profile": model,
            "baseline_run_id": run_ids['baseline'], "candidate_run_id": run_ids['candidate'],
            "baseline_variant": "source_retrieval" if focus else "evidence_summary" if retrieval else "baseline_full",
            "candidate_variant": "source_focus_v3" if focus else "source_retrieval" if retrieval else "evidence_summary",
            "baseline_label": "原文检索 v2" if focus else "上一版分批证据摘要" if retrieval else "原完整上下文策略",
            "candidate_label": "原文证据聚焦 v3" if focus else "原文检索与证据核对" if retrieval else "分批原文证据摘要",
            "sample_count": count, "completed_count": completed, "repeats": repeats,
            "collection_complete": complete, "quality_valid": quality, "acceptance_passed": None,
            "completed_at": stamp if complete else None, "updated_at": stamp, "status": status,
            "cost_known": cost_known, "actual_cost_usd": actual if cost_known else None,
            "known_cost_usd": known, "pending_reserved_usd": pending,
            "metrics": metrics, **comparison, "limitations_zh": limitations_zh, "limitations_en": limitations_en,
            "source_sha256": hashlib.sha256(raw).hexdigest()}


__all__ = ["project_context_optimization"]
