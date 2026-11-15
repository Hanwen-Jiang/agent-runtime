"""Allowlisted public summaries for Runtime ContextBuilder ablations.

This format intentionally remains separate from pure-model capability scores.
Completed and explicitly incomplete summaries are projected; source documents, answers,
predictions, signed URLs and billing ledgers never leave the experiment store.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any

SCHEMA = "longbench-context-ablation-v1"
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
REVISION = re.compile(r"^[0-9a-f]{7,40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
BLOCKED_REASONS = {"gateway_model_usage_pending", "budget_pending", "budget_exhausted", "gateway_unavailable", "pinned_model_mismatch", "operator_paused"}
METRICS = (
    ("accuracy", "答题准确率", "Answer accuracy", "rate"),
    ("answered_count", "成功返回答案的题数", "Items with an answer", "number"),
    ("budget_exceeded_count", "答题预算超限题数（计 0 分）", "Answer-budget failures (scored zero)", "number"),
    ("answer_format_failure_count", "答案格式失败题数", "Answer-format failures", "number"),
    ("infrastructure_error_count", "基础设施异常题数", "Items with infrastructure errors", "number"),
    ("input_tokens", "输入 token 总量", "Total input tokens", "number"),
    ("answer_input_tokens", "答题输入 token", "Answer input tokens", "number"),
    ("helper_input_tokens", "摘要与检查输入 token", "Summarization/check input tokens", "number"),
    ("output_tokens", "输出 token 总量", "Total output tokens", "number"),
    ("total_tokens", "总 token 用量", "Total tokens", "number"),
    ("context_tokens_mean", "答题上下文平均 token 数", "Mean answer-context tokens", "number"),
    ("elapsed_seconds", "总耗时", "Total elapsed time", "seconds"),
    ("cost_usd", "模型调用费用", "Model call cost", "usd"),
    ("compression_calls", "压缩调用次数", "Compression calls", "number"),
    ("summary_triggered_count", "成功应用摘要的题数", "Items successfully applying summarization", "number"),
    ("probe_count", "事实检查次数", "Fact checks", "number"),
    ("probe_failures", "事实检查未通过次数", "Failed fact checks", "number"),
    ("restored_item_count", "发生原文回补的题数", "Items with source restoration", "number"),
)


def _text(value: Any, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError("invalid public experiment identity")
    return value


def _number(value: Any, *, ratio: bool = False) -> int | float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= (1 if ratio else 2**53 - 1):
        raise ValueError("invalid public experiment metric")
    return value


def _flag(value: Any) -> bool | None:
    if value is not None and type(value) is not bool:
        raise ValueError("invalid public experiment status")
    return value


def project_longbench_summary(raw: bytes) -> dict[str, Any] | None:
    report = json.loads(raw)
    if not isinstance(report, dict) or report.get("schema") != SCHEMA:
        raise ValueError("unsupported LongBench summary")
    if report.get("evaluation_type") != "ablation" or report.get("evaluation_scope") != "component":
        raise ValueError("LongBench context comparison must be a component ablation")
    count, completed, repeats = report.get("sample_count"), report.get("completed_count"), report.get("repeats")
    if type(count) is not int or not 0 < count <= 10000 or type(repeats) is not int or not 0 < repeats <= 100:
        raise ValueError("invalid LongBench cohort size")
    complete = _flag(report.get("collection_complete"))
    if complete is None or type(completed) is not int or not 0 <= completed <= count:
        raise ValueError("invalid LongBench completion count")
    status = report.get("status", "completed" if complete else "running")
    if status not in {"blocked", "running", "completed"} or (status == "completed") != complete or complete and completed != count:
        raise ValueError("inconsistent LongBench completion status")
    blocked_reason = report.get("blocked_reason")
    if blocked_reason is not None and blocked_reason not in BLOCKED_REASONS:
        raise ValueError("unknown LongBench blocked reason")
    if status == "blocked" and blocked_reason is None:
        raise ValueError("blocked LongBench run requires a reason")
    identity = _text(report.get("id"), 128)
    if not IDENTIFIER.fullmatch(identity) or report.get("comparison_id") != identity:
        raise ValueError("invalid LongBench comparison identity")
    revision = _text(report.get("runtime_revision"), 40)
    if not REVISION.fullmatch(revision):
        raise ValueError("missing frozen Runtime revision")
    hashes = {key: _text(report.get(key), 64) for key in ("runtime_source_sha256", "config_sha256", "cohort_sha256")}
    if any(not SHA256.fullmatch(value) for value in hashes.values()):
        raise ValueError("invalid LongBench provenance digest")
    timestamp = _text(report.get("completed_at") if complete else report.get("updated_at"), 64)
    if datetime.fromisoformat(timestamp.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("missing LongBench completion timezone")
    if report.get("dataset_name") != "LongBench v2":
        raise ValueError("unexpected LongBench dataset")
    metadata = {key: _text(report.get(key), 256 if key == "model_profile" else 128)
                for key in ("dataset_version", "model_profile", "protocol")}
    repair = None
    repair_zh: list[str] = []
    repair_en: list[str] = []
    if metadata["protocol"] == "longbench-context-limit-repair-v1":
        value = report.get("repair")
        original = _text(report.get("original_comparison_id"), 128)
        if (not isinstance(value, dict) or report.get("budget_policy") != "model_window_only"
                or not IDENTIFIER.fullmatch(original) or original == identity
                or value.get("merge_policy") != "replace_only_prior_budget_exceeded"
                or "experiment_budget_usd" not in value or value["experiment_budget_usd"] is not None):
            raise ValueError("invalid selective LongBench repair identity")
        selected, replaced, unchanged = (value.get(key) for key in ("selected_arms", "replaced_arms", "unchanged_arms"))
        if (any(type(n) is not int for n in (selected, replaced, unchanged))
                or not 0 < selected <= count * 2 or not 0 <= replaced <= selected
                or unchanged != count * 2 - selected or complete and replaced != selected):
            raise ValueError("invalid selective LongBench repair counts")
        window = value.get("model_context_window_tokens")
        if type(window) is not int or not 0 < window <= 10_000_000:
            raise ValueError("missing model window for selective LongBench repair")
        repair = {"selected": selected, "replaced": replaced, "unchanged": unchanged, "window": window}
        repair_zh = [
            f"这是 {original} 的超限分支补跑合并：只补跑原先因答题上下文上限失败的 {selected} 个分支，已合并 {replaced} 个；其余 {unchanged} 个分支沿用原结果，原始记录均保留。",
            "此次取消实验自行设置的答题容量限制和美元额度，仅保留模型本身的上下文窗口。原有压缩触发条件不变；这不是40题两臂全部重新执行的统一新实验。",
            f"补跑模型的上下文窗口为 {window:,} tokens；最终答题可用空间按该窗口扣除必要输出余量，不能解释成仍受8192-token实验上限约束。",
        ]
        repair_en = [
            f"Selective repair merged with {original}: only {selected} arms previously rejected by the answer-context cap are rerun; {replaced} replacements are merged and {unchanged} original arms are retained. Original records are preserved.",
            "The experiment-imposed answer-capacity and USD caps are removed. Only the model context window remains, with the original compaction triggers unchanged. This is not a new uniform rerun of both arms on all 40 questions.",
            f"The rerun model context window is {window:,} tokens; usable answer input reserves necessary output space within it. It is not the previous 8192-token experimental cap.",
        ]
        triggers = [value.get("mask_trigger_tokens"), value.get("summarize_trigger_tokens")]
        if any(v is not None for v in triggers):
            if any(_number(v) is None for v in triggers) or not 0 < triggers[0] < triggers[1] <= window:
                raise ValueError("invalid selective LongBench compaction triggers")
            repair_zh.append(f"省略早期输出和摘要的绝对触发阈值分别保持为 {triggers[0]:g} / {triggers[1]:g} 个Runtime估算token。")
            repair_en.append(f"The absolute masking/summarization triggers remain {triggers[0]:g} / {triggers[1]:g} Runtime-estimated tokens.")
    elif report.get("repair") is not None or report.get("budget_policy") == "model_window_only":
        raise ValueError("selective repair requires its own protocol version")
    arms = report.get("arms")
    if not isinstance(arms, dict) or set(arms) != {"mask_only", "full"}:
        raise ValueError("missing LongBench comparison arms")
    run_ids = {}
    for name, arm in arms.items():
        if not isinstance(arm, dict) or not IDENTIFIER.fullmatch(_text(arm.get("run_id"), 128)):
            raise ValueError("invalid LongBench arm identity")
        run_ids[name] = arm["run_id"]
    if len(set(run_ids.values())) != 2:
        raise ValueError("LongBench arms share a run ID")
    incomplete_usage = {name: (_number(arm.get("infrastructure_error_count")) or 0) > 0 or
                        arm.get("usage_known") is False or arm.get("elapsed_known") is False
                        for name, arm in arms.items()}
    has_infrastructure_errors = any(incomplete_usage.values())
    quality_valid = _flag(report.get("quality_valid")) if complete and not has_infrastructure_errors else False
    lower_bound_labels = {
        "input_tokens": ("已记录输入 token（下界）", "Recorded input tokens (lower bound)"),
        "answer_input_tokens": ("已记录答题输入 token（下界）", "Recorded answer input tokens (lower bound)"),
        "helper_input_tokens": ("已记录摘要与检查输入 token（下界）", "Recorded summarization/check input tokens (lower bound)"),
        "output_tokens": ("已记录输出 token（下界）", "Recorded output tokens (lower bound)"),
        "total_tokens": ("已记录 token 总量（下界）", "Recorded total tokens (lower bound)"),
        "elapsed_seconds": ("已记录耗时（下界）", "Recorded elapsed time (lower bound)"),
        "cost_usd": ("已结算调用费用（下界）", "Settled model-call cost (lower bound)"),
    }
    metrics = []
    for key, zh, en, unit in METRICS:
        values = [_number(arms[name].get(key), ratio=unit == "rate") for name in ("mask_only", "full")]
        if has_infrastructure_errors and key in lower_bound_labels:
            zh, en = lower_bound_labels[key]
            # Failed calls may carry placeholder zeroes. Unknown usage/duration
            # must not be displayed as an observed zero for the affected arm.
            values = [None if incomplete_usage[name] and value == 0 else value
                      for name, value in zip(("mask_only", "full"), values)]
        if key == "accuracy" and has_infrastructure_errors:
            # A failed request supplied no model answer. Do not turn it into a
            # wrong answer by exposing correct/completed as an accuracy. Keep
            # independently verifiable integer counts without changing raw data.
            counts = []
            for value in values:
                if value is None:
                    counts.append(None)
                    continue
                correct = value * completed
                rounded = round(correct)
                if not math.isclose(correct, rounded, rel_tol=0, abs_tol=1e-6):
                    raise ValueError("accuracy does not encode an integer paired correct count")
                counts.append(rounded)
            if any(value is not None for value in counts):
                metrics.append({"key": "correct_count", "label_zh": "已判定答对题数",
                                "label_en": "Confirmed correct answers", "unit": "number",
                                "baseline": counts[0], "candidate": counts[1]})
            continue
        if key == "infrastructure_error_count":
            zh, en = "无有效结果／待补跑题数", "Items without valid results / awaiting rerun"
        if key == "accuracy" and quality_valid is not True:
            zh, en = "已配对队列答对比例（描述性）", "Paired-cohort correct fraction (descriptive)"
        if key == "accuracy" and complete and any(value is None for value in values):
            raise ValueError("missing LongBench accuracy")
        if any(value is not None for value in values):
            metrics.append({"key": key, "label_zh": zh, "label_en": en, "unit": unit,
                            "baseline": values[0], "candidate": values[1]})
    cost_known = _flag(report.get("cost_known")) if complete else False
    actual, known = _number(report.get("actual_cost_usd")), _number(report.get("known_cost_usd"))
    pending = _number(report.get("pending_reserved_usd"))
    if pending is not None and pending > 0:
        cost_known = False
    if cost_known is True and actual is None:
        raise ValueError("settled LongBench cost is missing")
    paired = report.get("paired") if quality_valid is True else None
    pair_values = {}
    if paired is not None:
        if not isinstance(paired, dict):
            raise ValueError("invalid paired comparison")
        for source, target in (("gained", "paired_gained"), ("lost", "paired_lost")):
            value = paired.get(source)
            if type(value) is not int or not 0 <= value <= count * repeats:
                raise ValueError("invalid paired outcome count")
            pair_values[target] = value
        delta, interval = paired.get("accuracy_delta"), paired.get("accuracy_delta_ci95")
        if type(delta) not in (int, float) or not math.isfinite(delta) or not -1 <= delta <= 1:
            raise ValueError("invalid paired accuracy delta")
        if not isinstance(interval, list) or len(interval) != 2 or any(type(x) not in (int, float) or not math.isfinite(x) or not -1 <= x <= 1 for x in interval) or interval[0] > interval[1]:
            raise ValueError("invalid paired interval")
        pair_values.update(paired_accuracy_delta=delta, paired_ci95_low=interval[0], paired_ci95_high=interval[1],
                           paired_mcnemar_p=_number(paired.get("mcnemar_exact_two_sided_p"), ratio=True))
    answer_budget = _number(report.get("answer_context_budget_tokens"))
    return {
        "id": identity, "kind": "context-ablation", "evaluation_type": "ablation", "evaluation_scope": "component",
        "title_zh": "LongBench v2 · 上下文超限补跑合并" if repair else "LongBench v2 · Runtime 上下文策略消融",
        "title_en": "LongBench v2 · Context-limit repair merge" if repair else "LongBench v2 · Runtime context ablation",
        "comparison_id": identity, "runtime_revision": revision, **hashes, **metadata,
        "dataset_name": "LongBench v2", "baseline_run_id": run_ids["mask_only"], "candidate_run_id": run_ids["full"],
        "baseline_variant": "mask_only", "candidate_variant": "full", "completed_at": timestamp if complete else None,
        "updated_at": timestamp, "status": status, "blocked_reason": blocked_reason,
        "sample_count": count, "completed_count": completed, "repeats": repeats, "collection_complete": complete,
        "answer_context_budget_tokens": answer_budget, **pair_values,
        "quality_valid": quality_valid, "acceptance_passed": _flag(report.get("acceptance_passed")) if quality_valid is True else None,
        "cost_known": cost_known, "actual_cost_usd": actual if cost_known is True else None, "known_cost_usd": known,
        "pending_reserved_usd": pending,
        "baseline_label": "仅省略早期工具输出", "candidate_label": "完整上下文管理",
        "metrics": metrics, "source_sha256": hashlib.sha256(raw).hexdigest(),
        "limitations_zh": ([] if complete else [
            f"目前仅 {completed}/{count} 道题完成配对，以下是暂定观测，不作为完整实验结论或提升证据。",
            "待对账预留金额不是已结算费用，不能按零费用处理；仅展示已结算费用下界。",
        ]) + ([
            "存在基础设施异常调用，其 token、费用和耗时记录不完整；表中用量与耗时仅为已记录下界，未知值不作为实际零值。",
        ] if has_infrastructure_errors else []) + [
            (f"当前有 {completed} 道题完成终态配对（完整计划为 {count} 道）；调用异常没有有效答案，单列为无有效结果／待补跑，不计作答错。存在这类异常时不展示准确率、提升或置信区间，也不以两臂各自不同的有效题数作分母比较。"
             if has_infrastructure_errors else
             f"答对比例的分母是当前已完成终态配对的 {completed} 道题（完整计划为 {count} 道）。" + ("当前队列描述性结果不作为完整有效质量结论。" if quality_valid is not True else "")),
        ] + repair_zh + ([
            f"合并结果仍覆盖原定 {count} 道题、每题两种策略；仅超限分支使用取消实验硬上限后的补跑结果。",
        ] if repair else [
            f"固定 {count} 道题、每臂 {repeats} 次，比较同一 Runtime 版本、模型与上下文预算下的两种策略。",
        ]) + [
            "通过 Runtime ContextBuilder 处理原文后答题；这是组件消融，不是纯模型官方榜单分数，也不是 Temporal 端到端任务评测。",
            "摘要、事实检查与原文回补是否实际触发，以调用和触发计数为准。",
        ] + ([
            "补跑与保留结果的调用用量分别留痕；原失败调用及补跑调用的费用不能遗漏或重复计入。仍有基础设施异常时，合并结果仅用于描述，不作为完整有效质量结论。",
        ] if repair else [
            "上下文预算约束最终答题输入；摘要与检查可读取更长原文，其 token 与费用全部计入。预算超限按策略失败计 0 分，题目仍保留在分母中。",
            "8192 token 是 Runtime 按字符估算的答题上下文预算，不是供应商分词器的精确上限；供应商实计 token 可能更高。",
        ]) + [
            "这是固定样本上的配对实验；单轮结果与区间不代表所有长上下文任务的普遍收益。",
        ],
        "limitations_en": ([] if complete else [
            f"Only {completed}/{count} questions have completed both arms. Values are provisional observations, not full-experiment conclusions or improvement evidence.",
            "Pending reservations are not settled charges and must not be treated as zero cost; only the settled lower bound is shown.",
        ]) + ([
            "Infrastructure-error calls have incomplete token, cost and duration records. Usage and elapsed values are recorded lower bounds; unknown values are not observed zeroes.",
        ] if has_infrastructure_errors else []) + [
            (f"There are {completed} terminal pairs from {count} planned questions. Request failures have no valid answer and are listed separately as awaiting rerun, not marked incorrect. Accuracy, gains and confidence intervals are withheld while such failures remain; the two arms are not compared using different valid-item denominators."
             if has_infrastructure_errors else
             f"The correct-fraction denominator is the {completed} currently completed terminal pairs ({count} planned)." + (" Current descriptive cohort results are not a complete valid quality conclusion." if quality_valid is not True else "")),
        ] + repair_en + ([
            f"Merged results still cover the original {count} questions and both strategies; only former cap-failed arms use rerun outcomes without the experimental hard cap.",
        ] if repair else [
            f"{count} fixed questions and {repeats} repeat(s) per arm; both arms share Runtime revision, model and context budget.",
        ]) + [
            "Documents pass through Runtime ContextBuilder before answering. This component ablation is neither a pure-model leaderboard score nor a Temporal end-to-end task evaluation.",
            "Summarization, fact checks and restoration are evidenced by their actual trigger counts.",
        ] + ([
            "Rerun and retained-call usage preserve separate provenance; original failed-call and rerun costs must neither disappear nor be double counted. With infrastructure failures, merged scores remain descriptive rather than complete valid quality conclusions.",
        ] if repair else [
            "The context budget applies to final answering. Summarization/checks may read longer source material; all their tokens and costs are counted. Budget failures score zero without removing questions from the denominator.",
            "The 8192-token budget uses Runtime's character-based estimate, not an exact provider-tokenizer limit; actual provider token counts may be higher.",
        ]) + [
            "This is a fixed-cohort paired experiment; a single round and its interval do not establish a general benefit across all long-context tasks.",
        ],
    }
