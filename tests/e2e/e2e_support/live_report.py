"""T-13 的端到端日志（docs/plan/live-e2e.md 第 2 节第 5 条）：一次 live 用例跑完写一份，放在 tests/e2e/.live-reports/<时间>-<名字>/（不提交）。

- run.json：Task 的结局、SSE 事件（类型、id、载荷）、事件历史里每个 activity 的类型、步骤 id、attempt 与结局、
  工具调用（工具名与结局，参数截断）、计量记录、按标价的合计成本，以及缓存命中率（cache_read 占输入 token 的比例，按调用的种类分开）。
- run.md：给人看的时间线与汇总。
写之前把已知的密钥换成 ***；计量记录本来就不含消息内容与密钥。
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from e2e_support.api import SseEvent
from e2e_support.history import HistoryReport, tool_calls

ARGS_LIMIT = 300


def call_kind(node_id: str) -> str:
    """计量记录按调用的种类分组：Planner（triage、plan）、Reviewer（review）、tool_loop 的一步（<Goal>.<序号>）、Context Builder、其他。"""
    if node_id in ("triage", "plan"):
        return "planner"
    if node_id == "review":
        return "reviewer"
    if "#" in node_id:
        return "context_builder"
    if "." in node_id:
        return "tool_loop"
    return "node"


def cache_read_ratio(records: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in records:
        kind = call_kind(str(r.get("node_id") or ""))
        totals[kind][0] += int(r.get("cache_read_tokens") or 0)
        totals[kind][1] += int(r.get("input_tokens") or 0)
        totals["all"][0] += int(r.get("cache_read_tokens") or 0)
        totals["all"][1] += int(r.get("input_tokens") or 0)
    return {k: round(c / i, 4) if i else 0.0 for k, (c, i) in sorted(totals.items())}


def _status(events: list[SseEvent]) -> str:
    finished = [e for e in events if e.event == "task_finished"]
    return str(finished[-1].data.get("payload", {}).get("value", {}).get("status", "")) if finished else ""


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= ARGS_LIMIT else text[:ARGS_LIMIT] + "…"


def write_run_report(
    directory: Path,
    *,
    name: str,
    task_id: str,
    events: list[SseEvent],
    history: HistoryReport,
    metering: list[dict[str, Any]],
    extra: dict[str, Any],
    secrets: list[str],
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    cost = sum(int(r.get("cost_usd_micros") or 0) for r in metering) / 1_000_000
    calls = [
        {"call_id": a.node_id, "tool": (a.input or {}).get("tool"), "args": _short((a.input or {}).get("args")), "attempts": a.attempts, "outcome": a.outcome}
        for a in tool_calls(history)
    ]
    data = {
        "name": name,
        "task_id": task_id,
        "status": _status(events),
        "events": [{"id": e.id, "event": e.event, "data": e.data} for e in events],
        "activities": [{"type": a.activity_type, "step": a.node_id, "round": a.round_seq, "attempts": a.attempts, "outcome": a.outcome} for a in history.activities],
        "tool_calls": calls,
        "metering": metering,
        "cost_usd": round(cost, 6),
        "cache_read_ratio": cache_read_ratio(metering),
        **extra,
    }
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)

    lines = [f"# T-13 端到端日志：{name}", "", f"- Task：`{task_id}`，结局 `{data['status']}`", f"- 按标价合计 ${cost:.4f}，模型调用 {len(metering)} 次"]
    lines.append("- 缓存命中率（cache_read ÷ 输入）：" + "，".join(f"{k} {v:.0%}" for k, v in data["cache_read_ratio"].items()))
    for k, v in extra.items():
        lines.append(f"- {k}：{v}")
    lines += ["", "## SSE 事件", "", "| id | 事件 | 要点 |", "| --- | --- | --- |"]
    for e in events:
        value = (e.data.get("payload") or {}).get("value") or {}
        brief = {k: value[k] for k in ("round_seq", "verdict", "node_id", "status", "question_id") if k in value}
        lines.append(f"| {e.id} | {e.event} | `{_short(brief)}` |")
    lines += ["", "## 事件历史里的 activity", "", "| 类型 | 步骤 | Round | attempt | 结局 |", "| --- | --- | --- | --- | --- |"]
    for a in data["activities"]:
        lines.append(f"| {a['type']} | {a['step'] or ''} | {a['round'] if a['round'] is not None else ''} | {a['attempts']} | {a['outcome']} |")
    lines += ["", "## 工具调用", "", "| 调用 | 工具 | 参数 | attempt | 结局 |", "| --- | --- | --- | --- | --- |"]
    for c in calls:
        lines.append(f"| {c['call_id']} | {c['tool']} | `{c['args']}` | {c['attempts']} | {c['outcome']} |")
    lines += ["", "## 模型调用（计量）", "", "| 步骤 | 种类 | Profile | 输入 | 缓存读 | 输出 | 美元 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in metering:
        node = str(r.get("node_id") or "")
        lines.append(
            f"| {node} | {call_kind(node)} | {r.get('model_profile_id')} | {r.get('input_tokens')} | {r.get('cache_read_tokens', 0)} | "
            f"{r.get('output_tokens')} | {int(r.get('cost_usd_micros') or 0) / 1_000_000:.4f} |"
        )
    markdown = "\n".join(lines) + "\n"

    for s in secrets:
        if s:
            text = text.replace(s, "***")
            markdown = markdown.replace(s, "***")
    (directory / "run.json").write_text(text, "utf-8")
    (directory / "run.md").write_text(markdown, "utf-8")
    return directory
