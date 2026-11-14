"""Allowlisted same-cohort historical context comparison; never a causal ranking."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any

SCHEMA = "runtime-40-comparison-v1"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SHA = re.compile(r"^[a-f0-9]{64}$")
REV = re.compile(r"^[a-f0-9]{7,40}$")


def _text(value: Any, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(x) < 32 for x in value):
        raise ValueError("invalid context comparison text")
    return value


def _count(value: Any, maximum: int = 40) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("invalid context comparison count")
    return value


def _metric(value: Any) -> float | int | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 2**53 - 1:
        raise ValueError("invalid context comparison measurement")
    return value


def project_context_comparison(raw: bytes) -> dict[str, Any]:
    doc = json.loads(raw)
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError("invalid context comparison schema")
    ident = _text(doc.get("id"), 128)
    if not ident.startswith("longbench-context-") or not NAME.fullmatch(ident) or doc.get("comparison_id") != ident:
        raise ValueError("invalid context comparison identity")
    if doc.get("sample_count") != 40 or doc.get("dataset_name") != "LongBench v2":
        raise ValueError("historical comparison requires the original 40-question cohort")
    cohort = _text(doc.get("cohort_sha256"), 64)
    if not SHA.fullmatch(cohort):
        raise ValueError("invalid context comparison cohort hash")
    stamp = _text(doc.get("updated_at"), 64)
    if datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("missing comparison timezone")
    model = _text(doc.get("model_profile"))
    revision = _text(doc.get("runtime_revision"), 40)
    if not REV.fullmatch(revision):
        raise ValueError("invalid Runtime base revision")
    variants = doc.get("variants")
    if not isinstance(variants, list) or not 2 <= len(variants) <= 8:
        raise ValueError("comparison requires 2..8 variants")
    clean, seen = [], set()
    for variant in variants:
        if not isinstance(variant, dict):
            raise ValueError("invalid variant")
        name = _text(variant.get("id"), 128)
        if not NAME.fullmatch(name) or name in seen:
            raise ValueError("duplicate or invalid variant")
        seen.add(name)
        if variant.get("cohort_sha256") != cohort or variant.get("model_profile") != model or variant.get("planned_count") != 40:
            raise ValueError("variant cohort/model mismatch")
        completed, valid, correct, unavailable = (_count(variant.get(k)) for k in
            ("completed_count", "valid_count", "correct_count", "unscored_count"))
        if valid + unavailable != completed or correct > valid:
            raise ValueError("variant scoring counts are inconsistent")
        status = variant.get("status")
        if status not in {"not_started", "running", "completed"} or status == "completed" and completed != 40 or status == "not_started" and completed:
            raise ValueError("inconsistent variant state")
        clean.append({"id": name, "label_zh": _text(variant.get("label_zh"), 128),
            "label_en": _text(variant.get("label_en"), 128), "protocol": _text(variant.get("protocol"), 128),
            "source_run_id": _text(variant.get("source_run_id"), 128), "status": status,
            "planned_count": 40, "completed_count": completed, "valid_count": valid,
            "correct_count": correct, "unscored_count": unavailable,
            "wrong_count": valid - correct,
            "confirmed_share": correct / 40,
            "known_cost_usd": _metric(variant.get("known_cost_usd")),
            "pending_reserved_usd": _metric(variant.get("pending_reserved_usd")),
            "recorded_input_tokens": _metric(variant.get("recorded_input_tokens")),
            "selected_elapsed_seconds": _metric(variant.get("selected_elapsed_seconds"))})
    if "mask_only" not in seen:
        raise ValueError("the original mask-only baseline is required")
    baseline_correct = next(v["correct_count"] for v in clean if v["id"] == "mask_only")
    for v in clean:
        v["correct_delta"] = v["correct_count"] - baseline_correct
    metrics = []
    for key, zh, en, unit in (
        ("correct_count", "已确认答对题数", "Confirmed correct answers", "number"),
        ("confirmed_share", "已确认答对占计划题数", "Confirmed correct / planned questions", "rate"),
        ("correct_delta", "较原基线多答对题数", "Additional confirmed answers versus masking", "number"),
        ("wrong_count", "有效回答中答错题数", "Incorrect valid answers", "number"),
        ("valid_count", "已取得有效结果题数", "Questions with valid results", "number"),
        ("unscored_count", "无有效结果／待补跑题数", "Unscored / awaiting rerun", "number"),
        ("completed_count", "已有终态记录题数", "Questions with terminal records", "number"),
        ("planned_count", "同一题集计划题数", "Questions in the shared cohort", "number"),
        ("known_cost_usd", "已知调用费用（下界）", "Known call cost (lower bound)", "usd"),
        ("pending_reserved_usd", "待对账预占（非实际费用）", "Pending reservations (not settled cost)", "usd"),
        ("recorded_input_tokens", "已记录输入 token（下界）", "Recorded input tokens (lower bound)", "number"),
        ("selected_elapsed_seconds", "所选尝试耗时（非总墙钟时间）", "Selected-attempt duration (not wall time)", "seconds"),
    ):
        values = [None if v["status"] == "not_started" and key != "planned_count" else v.get(key) for v in clean]
        if any(value is not None for value in values):
            metrics.append({"key": key, "label_zh": zh, "label_en": en, "unit": unit, "values": values})
    complete = all(v["status"] == "completed" for v in clean)
    # This is a historical/cohort view. Different output/transport protocols must
    # not produce a paired improvement field or appear in the pure-model matrix.
    return {"id": ident, "comparison_id": ident, "kind": "context-cohort-comparison",
        "evaluation_type": "ablation", "evaluation_scope": "component",
        "title_zh": "LongBench v2 · 原40题基线与策略对比", "title_en": "LongBench v2 · Original 40-question strategy comparison",
        "dataset_name": "LongBench v2", "dataset_version": _text(doc.get("dataset_version"), 128),
        "runtime_revision": revision, "model_profile": model, "cohort_sha256": cohort,
        "protocol": "historical-shared-cohort-comparison-v1", "updated_at": stamp,
        "sample_count": 40, "completed_count": min(v["completed_count"] for v in clean),
        "collection_complete": complete, "status": "completed" if complete else "running",
        "quality_valid": None, "cost_known": None, "source_sha256": hashlib.sha256(raw).hexdigest(),
        "variants": [{k: v[k] for k in ("id", "label_zh", "label_en", "protocol", "source_run_id", "status")} for v in clean],
        "metrics": metrics,
        "limitations_zh": [
            f"原基线是同一40题上的“仅省略早期工具输出”：已确认答对{baseline_correct}题（{baseline_correct}/40，{baseline_correct / 40:.1%}）。占比只表示答对数占计划题数，调用异常仍未判定；差值不是排除协议差异后的因果提升。",
            "各列使用相同40个题号与固定Kimi模型。调用异常或模型切换单列为未判定；未开始/进行中的列不填造零分。",
            "原基线和完整策略来自超限补跑合并；证据摘要采用结构化选项输出与流式调用，未经压缩参考保留完整Runtime工具历史。配置并非全部相同，横向表只作同题诊断，不宣称纯压缩改动的因果提升。",
            "未经压缩参考也是Runtime组件实验，不是官方纯模型榜单成绩。20道独立验收集的配对结果另行展示，不与此40题开发对比混合。",
            "费用与用量按每列记录口径展示，可能含未知费用或未完成调用；不同列的耗时不是整轮实验墙钟时间。",
        ],
        "limitations_en": [
            f"The masking baseline confirms {baseline_correct}/40 correct ({baseline_correct / 40:.1%} of planned questions). Unavailable calls remain unscored; differences are not causal improvements across protocols.",
            "Columns share the same 40 questions and pinned Kimi model. Unknown and not-started outcomes are not fabricated as zero scores.",
            "Historical repaired runs and newer JSON/streaming runs differ in protocol. This is diagnostic, not a causal improvement claim. The uncompressed reference preserves Runtime tool history and is not an official model-only score.",
            "The separate 20-question validation comparison is not pooled into this development cohort. Costs and recorded duration retain their individual accounting scope.",
        ]}
