"""读 trace（附录 J.1；T-16 第 1、2 步）。

输入是 T-15 定下的 span：Task 级 invoke_workflow 与它下面的 runtime.round（Worker 收尾写回时打的，见
agentruntime_worker.reviewer.wrap_up.trace_task）。两种格式解析成同样的 SpanRecord：
- otlp_json：每行一个 OTLP JSON 的 ExportTraceServiceRequest（Collector 的 kafka 与 file 导出器写的；读主题时一条记录一行，见 stream）；
- console：控制台 Telemetry 的 JSON 行（只取 kind 为 span 的）。

归并（task_traces）：
- 只取指定租户的 span（按 span 上的 tenant_id，由 Telemetry 绑定时加上，调用方伪造不了）；其他租户的 invoke_workflow、
  runtime.round 只计数。
- 同一个 task_id 的 invoke_workflow 出现多次（至少一次投递、收尾写回被重试）只算一次：取 span_id 最小的那个，
  它下面的 Round 按 round_seq 去重。结果与读入的顺序无关。
- 缺任务族、结局或 task_id 的 Task 跳过；Stage 形状不是合法 JSON 的 Task 跳过；找不到所属 Task 的 Round 跳过。都计入 skipped。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

TASK_SPAN = "invoke_workflow"
ROUND_SPAN = "runtime.round"
FORMATS = ("otlp_json", "console")


class TraceFormatError(ValueError):
    """trace 的某一行（读主题时是某一条记录）不是合法的 JSON 或格式不对。line 是从 1 起的行号，不知道时为 0。"""

    def __init__(self, message: str, line: int = 0) -> None:
        super().__init__(message)
        self.line = line


@dataclass(frozen=True)
class SpanRecord:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoundTrace:
    seq: int
    verdict: str
    triage_level: str
    cost_usd: float
    shape: dict[str, Any] | None

    @property
    def planned(self) -> bool:
        """经过 Planner 的 Round 有 triage_level；原地重跑的没有。"""
        return bool(self.triage_level)


@dataclass(frozen=True)
class TaskTrace:
    tenant_id: str
    task_id: str
    task_family: str
    status: str
    recipe: str
    cost_usd: float
    rounds: tuple[RoundTrace, ...]
    # span 上记着成本（runtime.cost_usd）；没有时 cost_usd 按 0 填，但门槛不能当它是 0（R-06-B：成本未知的比较不自动晋级）。
    cost_known: bool = True


@dataclass
class TraceSet:
    tasks: list[TaskTrace]
    skipped: int = 0
    other_tenant_spans: int = 0
    duplicates: int = 0


def _any_value(value: Mapping[str, Any]) -> Any:
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "arrayValue" in value:
        return [_any_value(v) for v in value["arrayValue"].get("values", [])]
    return None


def _otlp_spans(doc: Any) -> list[SpanRecord]:
    if not isinstance(doc, Mapping):
        raise TraceFormatError("不是 JSON 对象")
    out = []
    for rs in doc.get("resourceSpans") or []:
        for ss in rs.get("scopeSpans") or []:
            for s in ss.get("spans") or []:
                attributes = {kv["key"]: _any_value(kv.get("value") or {}) for kv in s.get("attributes") or []}
                out.append(SpanRecord(s.get("name", ""), s.get("traceId", ""), s.get("spanId", ""), s.get("parentSpanId", "") or "", attributes))
    return out


def _console_span(doc: Any) -> list[SpanRecord]:
    if not isinstance(doc, Mapping):
        raise TraceFormatError("不是 JSON 对象")
    if doc.get("kind") != "span":
        return []
    return [SpanRecord(doc.get("name", ""), doc.get("trace_id", ""), doc.get("span_id", ""), doc.get("parent_span_id", "") or "", dict(doc.get("attributes") or {}))]


def read_spans(lines: Iterable[str], fmt: str = "otlp_json") -> list[SpanRecord]:
    """逐行解析；空行跳过，坏行报 TraceFormatError（带行号）。"""
    if fmt not in FORMATS:
        raise ValueError(f"不认识的格式 {fmt!r}：可选 {'、'.join(FORMATS)}")
    parse = _otlp_spans if fmt == "otlp_json" else _console_span
    spans: list[SpanRecord] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            spans.extend(parse(json.loads(line)))
        except (json.JSONDecodeError, TraceFormatError, KeyError, TypeError, ValueError) as exc:
            raise TraceFormatError(f"第 {number} 行不是合法的 {fmt} 记录：{type(exc).__name__}", number) from None
    return spans


def _round(span: SpanRecord, index: int) -> RoundTrace:
    a = span.attributes
    raw = a.get("runtime.stage_shape")
    shape = None
    if raw is not None:
        shape = json.loads(raw) if isinstance(raw, str) else None
        if not isinstance(shape, dict):
            raise ValueError("Stage 形状不是 JSON 对象")
    return RoundTrace(
        seq=int(a.get("round_seq") or index),
        verdict=str(a.get("verdict") or ""),
        triage_level=str(a.get("triage_level") or ""),
        cost_usd=float(a.get("runtime.cost_usd") or 0.0),
        shape=shape,
    )


def task_traces(spans: Iterable[SpanRecord], tenant_id: str) -> TraceSet:
    roots: dict[str, list[SpanRecord]] = {}
    rounds: dict[str, list[SpanRecord]] = {}
    result = TraceSet(tasks=[])
    for span in spans:
        if span.name not in (TASK_SPAN, ROUND_SPAN):
            continue
        if span.attributes.get("tenant_id") != tenant_id:
            result.other_tenant_spans += 1
            continue
        if span.name == TASK_SPAN:
            roots.setdefault(str(span.attributes.get("task_id") or ""), []).append(span)
        else:
            rounds.setdefault(span.parent_span_id, []).append(span)

    known_parents: set[str] = set()
    for task_id in sorted(roots):
        candidates = sorted(roots[task_id], key=lambda s: s.span_id)
        known_parents.update(s.span_id for s in candidates)
        result.duplicates += len(candidates) - 1
        root = candidates[0]
        a = root.attributes
        family, status = a.get("runtime.task_family"), a.get("runtime.task_status")
        if not task_id or not family or not status:
            result.skipped += 1
            continue
        seen: dict[int, RoundTrace] = {}
        try:
            for i, span in enumerate(sorted(rounds.get(root.span_id, []), key=lambda s: (int(s.attributes.get("round_seq") or 0), s.span_id)), start=1):
                r = _round(span, i)
                seen.setdefault(r.seq, r)
        except (ValueError, TypeError):
            result.skipped += 1
            continue
        result.tasks.append(
            TaskTrace(
                tenant_id=tenant_id,
                task_id=task_id,
                task_family=str(family),
                status=str(status),
                recipe=str(a.get("recipe") or ""),
                cost_usd=float(a.get("runtime.cost_usd") or 0.0),
                rounds=tuple(seen[k] for k in sorted(seen)),
                cost_known=a.get("runtime.cost_usd") not in (None, ""),
            )
        )
    result.skipped += sum(len(spans_) for parent, spans_ in rounds.items() if parent not in known_parents)
    return result
