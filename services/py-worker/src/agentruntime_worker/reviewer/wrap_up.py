"""收尾写回（附录 B.9）：归档 Task State、交经验与 Recipe 效果候选给 Memory Service、写 trace 指标与 Task、Round 两级 span（T-15）。

每个 Provider 客户端都按 Task 的租户绑定。Memory Service 经它的 gRPC 接口（SubmitCandidates）接收 Reviewer 提交的
经验候选，进入写入流水线（设计书 3.4）；开发与测试用 RecordingMemoryClient。

三件事各自独立：一件失败不影响另外两件，做完之后再报告失败（可重试）。重试时归档覆盖同一个对象，候选的 id
由租户、Task 与类别决定，不会重复。写回失败不改变 Task 的结局（Task 工作流已经先把结果交给了 Session）。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import payload_pb2, tenant_pb2
from agentruntime.memory.v1 import memory_pb2
from agentruntime_core.providers.object_store import ObjectStoreProvider
from agentruntime_core.providers.telemetry import TelemetryProvider
from agentruntime_core.tenancy import TenantContext
from temporalio import activity
from temporalio.exceptions import ApplicationError

from agentruntime_worker.activities.types import FinalizeTaskInput

MEMORY_ITEM_SCHEMA_ID = "https://agentruntime.example/schemas/memory_item.schema.json"
ARCHIVE_KEY = "tasks/{task_id}/task_state.json"
# 这一轮对话做过的事（docs/plan/feedback-2026-11-01.md N-03）：之后同一 Session 的追问读它（session/transcript.py）。
SESSION_RECORD_KEY = "tasks/{task_id}/session_record.json"
SESSION_RECORD_FACTS = 10
PROGRESS_VERDICTS = ("continue", "done")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ID_PATTERNS = {
    "tenant": re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$"),
    "workspace": re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$"),
    "user": re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$"),
    "session": re.compile(r"^ses_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
    "task": re.compile(r"^tsk_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
}


class MemoryClient(Protocol):
    async def submit_candidates(self, request: memory_pb2.SubmitCandidatesRequest) -> memory_pb2.SubmitCandidatesResponse: ...


class RecordingMemoryClient:
    """开发与测试用：记下每个请求，全部接受。"""

    def __init__(self) -> None:
        self.requests: list[memory_pb2.SubmitCandidatesRequest] = []

    async def submit_candidates(self, request: memory_pb2.SubmitCandidatesRequest) -> memory_pb2.SubmitCandidatesResponse:
        self.requests.append(request)
        return memory_pb2.SubmitCandidatesResponse(accepted=len(request.candidates))

    def items(self) -> list[dict[str, Any]]:
        """收到的全部候选（memory_item）。"""
        return [json_format.MessageToDict(c.memory_item.value) for r in self.requests for c in r.candidates]


@dataclass
class WrapUpReport:
    archive_uri: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


def _value(data: Any) -> struct_pb2.Value:
    value = struct_pb2.Value()
    json_format.ParseDict(data, value)
    return value


def _memory_id(*parts: str) -> str:
    """mem_ 加 26 位 ULID 字符：由内容决定，重试时不变。"""
    digest = hashlib.sha256("/".join(parts).encode()).digest()
    return "mem_" + "01234567"[digest[0] % 8] + "".join(_CROCKFORD[b % 32] for b in digest[1:26])


def _rounds(inp: FinalizeTaskInput) -> list[dict[str, Any]]:
    return [r for r in inp.task_state.get("rounds") or [] if isinstance(r, dict)]


def task_cost(inp: FinalizeTaskInput) -> float:
    return round(sum(float(r.get("cost_usd") or 0.0) for r in _rounds(inp)), 6)


def trace_metrics(inp: FinalizeTaskInput) -> dict[str, Any]:
    """写进 trace 的指标：成本、升级次数（没有进展的 Round）、各 Verdict 的分布（附录 B.9）。"""
    verdicts = Counter(str(r["verdict"]) for r in _rounds(inp) if r.get("verdict"))
    return {
        "cost_usd": task_cost(inp),
        "escalations": sum(n for v, n in verdicts.items() if v not in PROGRESS_VERDICTS),
        "verdicts": dict(sorted(verdicts.items())),
    }


async def trace_task(telemetry: Any, inp: FinalizeTaskInput, metrics: dict[str, Any]) -> None:
    """附录 J.1 的 Task 与 Round 两级 span（T-15）：收尾时事后补记，标 runtime.synthetic（时间戳不是 Task 的真实起止）。
    Round 的 runtime.stage_shape 是 Stage 的形状（verdicts.stage_shape），不含 inputs 与 rationale；Learning Pipeline 从这里读（T-16）。"""
    rounds = _rounds(inp)
    temporal: dict[str, Any] = {}
    if activity.in_activity():
        info = activity.info()
        temporal = {"temporal.workflow_id": info.workflow_id or "", "temporal.run_id": info.workflow_run_id or ""}
    attributes = {
        "task_id": inp.task_id,
        **temporal,
        "gen_ai.operation.name": "invoke_workflow",
        "recipe": str((inp.task_state.get("pinned") or {}).get("recipe") or ""),
        "runtime.task_family": inp.task_family,
        "runtime.task_status": inp.status,
        "runtime.cost_usd": metrics["cost_usd"],
        "runtime.rounds": len(rounds),
        "runtime.synthetic": True,
    }
    async with telemetry.start_span("invoke_workflow", attributes=attributes):
        for i, r in enumerate(rounds):
            round_attributes: dict[str, Any] = {
                "task_id": inp.task_id,
                "round_seq": int(r.get("seq") or i + 1),
                "runtime.cost_usd": float(r.get("cost_usd") or 0.0),
                "runtime.synthetic": True,
            }
            if r.get("verdict"):
                round_attributes["verdict"] = str(r["verdict"])
            if r.get("triage_level"):
                round_attributes["triage_level"] = str(r["triage_level"])
            shape = inp.stages[i].get("shape") if i < len(inp.stages) else None
            if shape is not None:
                round_attributes["runtime.stage_shape"] = json.dumps(shape, ensure_ascii=False, sort_keys=True)
            async with telemetry.start_span("runtime.round", attributes=round_attributes):
                pass


def _scope(inp: FinalizeTaskInput) -> dict[str, Any]:
    t = inp.tenant
    raw = {"tenant": t.tenant_id, "workspace": t.workspace_id, "user": t.user_id, "session": t.session_id or inp.task_state.get("session_id", ""), "task": inp.task_id}
    return {k: (v if v and _ID_PATTERNS[k].match(v) else None) for k, v in raw.items()}


def memory_candidates(inp: FinalizeTaskInput, *, today: str) -> list[dict[str, Any]]:
    """经验与 Recipe 效果的候选（memory_item）。done 交成功的经验，aborted 交失败的经验，两者都交 Recipe 的效果；
    取消与系统失败不是这次做法的经验，不交。候选里有用户的原话，按 pii 交给写入流水线先脱敏。"""
    kind = {"done": "success", "aborted": "failure"}.get(inp.status)
    if kind is None:
        return []
    state = inp.task_state
    scope = _scope(inp)
    goal = str(state.get("goal") or "")
    rounds = _rounds(inp)
    flow = " → ".join(str(r.get("stage_goal")) for r in rounds) or "（无）"
    verdict = inp.verdict or {}
    if kind == "success":
        title = f"做成过：{goal}"
        guidance = f"用 {len(rounds)} 个 Round 完成：{flow}；交付来自 {inp.deliver_node or '（未记录）'}。验收条件：{state.get('done_when', '')}"
    else:
        abort = verdict.get("abort") or {}
        title = f"没做成：{goal}"
        guidance = f"{verdict.get('reason') or '中止'}。完成了：{'、'.join(abort.get('completed') or []) or '无'}；没完成：{'、'.join(abort.get('not_completed') or []) or '无'}。经过：{flow}"
    items = [(kind, title, guidance)]
    recipe = (state.get("pinned") or {}).get("recipe")
    if recipe:
        stages = "；".join(f"{s.get('seq')}. {s.get('goal')}（{s.get('nodes')} 个 Node）" for s in inp.stages) or flow
        tiers = sorted({t for s in inp.stages for t in s.get("tiers") or []})
        verdicts = " → ".join(str(r.get("verdict")) for r in rounds if r.get("verdict"))
        items.append(
            (
                "recipe",
                f"Recipe {recipe} 的效果",
                f"结局 {inp.status}；Stage 划分：{stages}；Tier：{'、'.join(tiers) or '未记录'}；成本 ${task_cost(inp):.2f}；Verdict：{verdicts or '无'}",
            )
        )
    provenance: dict[str, Any] = {"source": "agent_reflection"}
    if scope["session"]:
        provenance["session"] = scope["session"]
    return [
        {
            "id": _memory_id(inp.tenant.tenant_id, inp.task_id, k),
            "scope": scope,
            "kind": k,
            "content": {"title": t[:200], "guidance": g},
            "provenance": provenance,
            "trust": 0.3,
            "validity": {"valid_from": today},
            "sensitivity": "pii",
            "embedding_version": "pending",
            "version": 1,
        }
        for k, t, g in items
    ]


def session_record(inp: FinalizeTaskInput) -> dict[str, Any]:
    """这一轮对话做过的事：各 Round 的目标与 Verdict、Reviewer 记下的事实、产物、实际调过的工具（作用对象已去凭据）。
    不含交付的正文（它在 task-service 的 tasks.outcome 里）。"""
    state = inp.task_state
    rounds = [
        {k: r[k] for k in ("seq", "stage_goal", "triage_level", "verdict", "tool_calls") if k in r}
        for r in state.get("rounds") or []
        if isinstance(r, dict)
    ]
    facts = [str(f) for f in state.get("facts") or []][-SESSION_RECORD_FACTS:]
    return {"status": inp.status, "rounds": rounds, "facts": facts, "artifacts": [str(a) for a in inp.artifacts], "actions": list(inp.actions)}


class WrapUp:
    def __init__(self, *, objects: ObjectStoreProvider, memory: MemoryClient, telemetry: TelemetryProvider) -> None:
        self.objects = objects
        self.memory = memory
        self.telemetry = telemetry

    async def finalize(self, inp: FinalizeTaskInput) -> WrapUpReport:
        t = inp.tenant
        tenant = TenantContext(tenant_id=t.tenant_id, user_id=t.user_id, workspace_id=t.workspace_id, session_id=t.session_id)
        errors: list[str] = []
        report = WrapUpReport(archive_uri="")

        objects = self.objects.bind(tenant)
        key = ARCHIVE_KEY.format(task_id=inp.task_id)
        try:
            data = json.dumps(inp.task_state, ensure_ascii=False, sort_keys=True).encode()
            await objects.put(key, data, content_type="application/json")
            report.archive_uri = objects.uri(key)
        except Exception as exc:  # noqa: BLE001 — 三件事各自独立，最后一并报告
            errors.append(f"归档 Task State 失败：{type(exc).__name__}")

        try:
            record = json.dumps(session_record(inp), ensure_ascii=False, sort_keys=True).encode()
            await objects.put(SESSION_RECORD_KEY.format(task_id=inp.task_id), record, content_type="application/json")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"写 Session 记录失败：{type(exc).__name__}")

        report.candidates = memory_candidates(inp, today=datetime.now(UTC).date().isoformat())
        if report.candidates:
            request = memory_pb2.SubmitCandidatesRequest(
                tenant=tenant_pb2.TenantContext(tenant_id=t.tenant_id, user_id=t.user_id, workspace_id=t.workspace_id, session_id=t.session_id),
                candidates=[
                    memory_pb2.MemoryCandidate(memory_item=payload_pb2.JsonPayload(schema_id=MEMORY_ITEM_SCHEMA_ID, value=_value(item)), source_task_id=inp.task_id)
                    for item in report.candidates
                ],
            )
            try:
                await self.memory.submit_candidates(request)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"提交经验候选失败：{type(exc).__name__}")

        report.metrics = trace_metrics(inp)
        try:
            telemetry = self.telemetry.bind(tenant)
            await trace_task(telemetry, inp, report.metrics)
            await telemetry.observe("task.cost_usd", report.metrics["cost_usd"], attributes={"status": inp.status})
            if inp.status == "done":
                await telemetry.observe("task.success_cost_usd", report.metrics["cost_usd"])
            await telemetry.count("task.escalations", report.metrics["escalations"], attributes={"status": inp.status})
            for verdict, n in report.metrics["verdicts"].items():
                await telemetry.count("task.verdicts", n, attributes={"verdict": verdict})
            await telemetry.flush()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"写 trace 指标失败：{type(exc).__name__}")

        if errors:
            # 可重试：归档覆盖同一个对象，候选 id 不变。
            raise ApplicationError("；".join(errors), type="write_back_failed")
        return report
