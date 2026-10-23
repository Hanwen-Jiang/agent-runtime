"""Hook 目录（附录 F.3 的默认 Hook 与策略声明的 Hook；细化见 docs/plan/policy-engine.md 第 2 节）。

每个 Hook 是纯函数：只看 HookContext（租户、Task、挂载点与 subject），返回 decision 等字段（hook、mount 由引擎补上）；
不调模型、不做网络 I/O（设计书 3.2.6 关键规则）。要留记录的 Hook（trace_result、checkpoint、audit_log）把记录放在
返回值的 "_record" 里，由引擎写进 Telemetry，Hook 本身不碰 I/O。

subject 的约定（Stage Runner 与 Task 工作流给出）：
- before_node：{"node", "budget": {"task_usd", "spent_usd"}, "write_serialized", "idempotency_key"（写操作）}
- after_node：{"node", "result"}
- before_stage：{"stage": {"seq", "goal", "exit_when", "budget"}, "budget"}
- before_finish：{"verdict", "deliver_node", "deliver_status", "deliverable", "output_schema"}
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.executor import keys
from agentruntime_worker.workflows import stage_plan as sp

# trace_result 写进 trace 的结果最多这么多字符（附录 D.4：结果写入 trace，便于复现）。
TRACE_LIMIT = 8 * 1024
REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class HookContext:
    """一次 Hook 执行看得到的东西：租户、Task、Round、挂载点与 subject。"""

    tenant: Tenant
    task_id: str
    round_seq: int
    mount: str
    subject: Mapping[str, Any]


@dataclass(frozen=True)
class HookSpec:
    """一个 Hook：能挂的挂载点与实现。"""

    mounts: frozenset[str]
    run: Callable[[HookContext], dict[str, Any]]


def allow() -> dict[str, Any]:
    return {"decision": "allow"}


def deny(reason: str) -> dict[str, Any]:
    return {"decision": "deny", "reason": reason}


def _node(ctx: HookContext) -> Mapping[str, Any]:
    return ctx.subject.get("node") or {}


def _effect(node: Mapping[str, Any]) -> str:
    """Atomic Node 取 effect；Goal Node 取 effect_ceiling（没有时按只读）。"""
    if node.get("kind") == "goal":
        return str(node.get("effect_ceiling") or "read")
    return str(node.get("effect") or "none")


def _usd(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


# --- 默认 Hook ---


def budget_check(ctx: HookContext) -> dict[str, Any]:
    """Task 的预算用完了，或本 Node / 本 Stage 的预算超过剩余，就拒绝。Task 没有预算时放行。"""
    budget = ctx.subject.get("budget") or {}
    task_usd = _usd(budget.get("task_usd"))
    if task_usd is None:
        return allow()
    spent = _usd(budget.get("spent_usd")) or 0.0
    remaining = task_usd - spent
    if remaining <= 0:
        return deny(f"Task 的预算 {task_usd:g} 美元已经用完（已花 {spent:.4f}）")
    if ctx.mount == "before_stage":
        what, need = "本 Stage", _usd(((ctx.subject.get("stage") or {}).get("budget") or {}).get("usd"))
    else:
        node = _node(ctx)
        node_budget = node.get("budget") or {}
        what = f"Node {node.get('id', '')}"
        need = _usd(node_budget.get("usd")) if node.get("kind") == "goal" else _usd(node_budget.get("max_cost_usd"))
    if need is not None and need > remaining + 1e-9:
        return deny(f"{what} 的预算 {need:g} 美元超过 Task 剩余的 {remaining:.4f} 美元")
    return allow()


def idempotency_check(ctx: HookContext) -> dict[str, Any]:
    """写操作的幂等键必须等于按 tenant_id、task_id、node_id 与输入算出的值（附录 D.4）。"""
    node = _node(ctx)
    if node.get("kind") == "goal" or _effect(node) not in sp.WRITE_EFFECTS:
        return allow()
    key = ctx.subject.get("idempotency_key")
    if not key:
        return deny(f"写操作 {node.get('id', '')} 缺幂等键")
    expected = keys.idempotency_key(ctx.tenant.tenant_id, ctx.task_id, str(node.get("id", "")), node.get("inputs") or {})
    if key != expected:
        return deny(f"写操作 {node.get('id', '')} 的幂等键不是按租户、Task、Node 与输入算出的")
    return allow()


def single_writer_lock(ctx: HookContext) -> dict[str, Any]:
    """写操作必须由 Stage Runner 串行化（同一时刻只允许一个节点写同一份共享产物，附录 D.4）。"""
    node = _node(ctx)
    if _effect(node) in sp.WRITE_EFFECTS and not ctx.subject.get("write_serialized"):
        return deny(f"写操作 {node.get('id', '')} 没有串行化，可能与别的写者同时写")
    return allow()


def trace_result(ctx: HookContext) -> dict[str, Any]:
    """read 的结果写进 trace（规范化 JSON，超过 TRACE_LIMIT 截断）。"""
    node = _node(ctx)
    text = keys.canonical_json(ctx.subject.get("result"))
    if len(text) > TRACE_LIMIT:
        text = text[:TRACE_LIMIT] + "…（截断）"
    return {**allow(), "_record": ("node.result", {"node_id": node.get("id", ""), "template": node.get("template", "")}, f"result {text}")}


def checkpoint(ctx: HookContext) -> dict[str, Any]:
    """write_reversible 执行后记一条检查点：Node、结果摘要与模板（回滚方式登记在模板里）。"""
    node = _node(ctx)
    attrs = {"node_id": node.get("id", ""), "template": node.get("template", ""), "result_hash": keys.input_hash(ctx.subject.get("result"))}
    return {**allow(), "_record": ("policy.checkpoint", attrs, "")}


def audit_log(ctx: HookContext) -> dict[str, Any]:
    """审计：Node、模板、effect、role、输入与结果的哈希；不记原文。"""
    node = _node(ctx)
    attrs = {
        "node_id": node.get("id", ""),
        "template": node.get("template", ""),
        "effect": _effect(node),
        "role": node.get("role", ""),
        "input_hash": keys.input_hash(node.get("inputs") or {}),
        "result_hash": keys.input_hash(ctx.subject.get("result")),
    }
    return {**allow(), "_record": ("policy.audit", attrs, "")}


# --- 策略声明的 Hook ---

_PII = (
    re.compile(r"(?<![\dXx])\d{17}[\dXx](?![\dXx])"),  # 身份证号（先于手机号，免得被拆开）
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),  # 邮箱
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),  # 手机号
)


def _strings(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """JSON 文档里所有字符串叶子：(JSON Pointer, 值)。"""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(item, f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _strings(item, f"{path}/{i}")


def pii_redaction(ctx: HookContext) -> dict[str, Any]:
    """结果里的身份证号、邮箱、手机号换成掩码（modify）；没有就放行。"""
    patch = []
    for path, text in _strings(ctx.subject.get("result")):
        redacted = text
        for pattern in _PII:
            redacted = pattern.sub(REDACTED, redacted)
        if redacted != text:
            patch.append({"op": "replace", "path": path, "value": redacted})
    if not patch:
        return allow()
    return {"decision": "modify", "modification": {"target": "output", "patch": patch}}


def acceptance_recheck(ctx: HookContext) -> dict[str, Any]:
    """完成前复核：交付节点要成功，交付物不为空，并且合交付节点的 output_schema（内联的才校验）。"""
    s = ctx.subject
    node = s.get("deliver_node", "")
    if s.get("deliver_status") != "succeeded":
        return deny(f"交付节点 {node} 没有成功")
    deliverable = s.get("deliverable")
    if deliverable is None or deliverable in ({}, [], ""):
        return deny(f"交付节点 {node} 的交付物是空的")
    schema = s.get("output_schema")
    if isinstance(schema, Mapping):
        error = next(iter(Draft202012Validator(dict(schema)).iter_errors(deliverable)), None)
        if error is not None:
            where = "/".join(str(p) for p in error.absolute_path) or "根"
            return deny(f"交付物不合交付节点的 output_schema（{where}：{error.validator}）")
    return allow()


def _mounts(*names: str) -> frozenset[str]:
    return frozenset(names)


HOOKS: dict[str, HookSpec] = {
    "budget_check": HookSpec(_mounts("before_node", "before_stage"), budget_check),
    "idempotency_check": HookSpec(_mounts("before_node"), idempotency_check),
    "single_writer_lock": HookSpec(_mounts("before_node"), single_writer_lock),
    "trace_result": HookSpec(_mounts("after_node"), trace_result),
    "checkpoint": HookSpec(_mounts("after_node"), checkpoint),
    "audit_log": HookSpec(_mounts("after_node"), audit_log),
    "pii_redaction": HookSpec(_mounts("after_node"), pii_redaction),
    "acceptance_recheck": HookSpec(_mounts("before_finish"), acceptance_recheck),
}
