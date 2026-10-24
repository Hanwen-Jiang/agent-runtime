"""Policy Engine（设计书 3.2.6、附录 F；T-09，细化见 docs/plan/policy-engine.md）。

Worker 进程内的库：PolicyEngine.run_hooks 是 Stage Runner 与 Task 工作流调用的 local activity（names.RUN_HOOKS），
Worker 默认就用它（代替 S-04 的桩）。规则：

- 按顺序执行 HookCall.hooks，每个 Hook 只返回 allow、deny、modify、inject 之一（hook_result.schema.json）；
  遇到第一个 deny 就停，后面的不执行。前面 Hook 的 modify 先作用到 subject 上，后面的 Hook 看到的是改过的输入或结果
  （例如 trace_result 记的是 pii_redaction 脱敏之后的结果）。
- fail-closed：名字不认识、挂错挂载点、Hook 抛异常、返回的结果不认识或不完整，一律按 deny；异常只记类型，不记原文（可能带数据）。
- 每次 deny 写一条审计记录（policy.decision）；trace_result、checkpoint、audit_log 的记录也由这里写进 Telemetry。
  记录里只有 Node、模板、effect、哈希与理由，不含输入与结果的原文（trace_result 写结果原文是附录 D.4 的要求）。
  Telemetry 写不出去（例如 Collector 不可用）时只记一条日志，Hook 的结果照常返回：after_node 时副作用已经发生，卡住 Task 拦不住什么
  （R-01 评审第 4 节第 1 条）。
- Hook 是纯函数（hooks.py），不调模型、不做网络 I/O；Telemetry 只在引擎里用。
"""

from __future__ import annotations

import contextlib
import copy
import logging
import re
from collections.abc import AsyncIterator, Mapping
from typing import Any

from temporalio import activity

from agentruntime_core.providers.telemetry import Telemetry, TelemetryProvider
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker import names
from agentruntime_worker.activities.types import HookCall
from agentruntime_worker.policy.hooks import HOOKS, HookContext, HookSpec, deny
from agentruntime_worker.workflows import stage_plan as sp

__all__ = ["HOOKS", "HookContext", "HookSpec", "PolicyEngine"]

log = logging.getLogger(__name__)

# 与 defs.schema.json 的 hook_name 一致；不合格的名字在结果里写成 invalid_hook_name。
_HOOK_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
DECISIONS = ("allow", "deny", "modify", "inject")


def _malformed(outcome: Mapping[str, Any]) -> str:
    """Hook 的结果哪里不合 hook_result.schema.json 的要求；合格时为空。"""
    decision = outcome.get("decision")
    if decision not in DECISIONS:
        return f"不认识的结果 {decision!r}"
    if decision == "deny" and not outcome.get("reason"):
        return "deny 没有理由"
    if decision == "modify":
        modification = outcome.get("modification")
        if not isinstance(modification, Mapping) or modification.get("target") not in ("input", "output") or not isinstance(modification.get("patch"), list):
            return "modify 缺少 target 或 patch"
    if decision == "inject" and not outcome.get("injection"):
        return "inject 缺少 injection"
    return ""


def _modified(subject: Mapping[str, Any], modification: Mapping[str, Any]) -> dict[str, Any]:
    """把 modify 作用到 subject 上（input 改 node.inputs，output 改 result），给后面的 Hook 看。"""
    out = copy.deepcopy(dict(subject))
    if modification.get("target") == "input":
        node = dict(out.get("node") or {})
        node["inputs"] = sp.apply_patch(node.get("inputs", {}), modification["patch"])
        out["node"] = node
    else:
        out["result"] = sp.apply_patch(out.get("result"), modification["patch"])
    return out


class PolicyEngine:
    """hooks 默认是 HOOKS（hooks.py）；telemetry 为 None 时不留记录。"""

    def __init__(self, *, telemetry: TelemetryProvider | None = None, hooks: Mapping[str, HookSpec] | None = None) -> None:
        self.telemetry = telemetry
        self.hooks = dict(HOOKS if hooks is None else hooks)

    @activity.defn(name=names.RUN_HOOKS)
    async def run_hooks(self, call: HookCall) -> list[dict[str, Any]]:
        subject: Mapping[str, Any] = call.subject
        results: list[dict[str, Any]] = []
        records: list[tuple[str, dict[str, Any], str]] = []
        for name in call.hooks:
            ctx = HookContext(tenant=call.tenant, task_id=call.task_id, round_seq=call.round_seq, mount=call.mount, subject=subject)
            outcome = self._evaluate(name, ctx)
            record = outcome.pop("_record", None)
            if record is not None:
                records.append(record)
            result = {"hook": name if _HOOK_NAME.match(name) else "invalid_hook_name", "mount": call.mount, **outcome}
            results.append(result)
            if result["decision"] == "deny":
                node = subject.get("node") or {}
                attrs = {"hook": result["hook"], "mount": call.mount, "decision": "deny", "reason": result["reason"], "node_id": node.get("id", "")}
                records.append(("policy.decision", attrs, ""))
                break
            if result["decision"] == "modify":
                subject = _modified(subject, result["modification"])
        await self._record(call, records)
        return results

    def _evaluate(self, name: str, ctx: HookContext) -> dict[str, Any]:
        spec = self.hooks.get(name)
        if spec is None:
            return deny(f"不认识的 Hook {name}")
        if ctx.mount not in spec.mounts:
            return deny(f"Hook {name} 不能挂在 {ctx.mount}")
        try:
            outcome = dict(spec.run(ctx))
        except Exception as err:  # noqa: BLE001 — Hook 出错按拒绝处理（fail-closed）
            return deny(f"Hook {name} 执行出错（{type(err).__name__}）")
        problem = _malformed(outcome)
        if problem:
            return deny(f"Hook {name} 的结果不合格（{problem}）")
        if outcome["decision"] == "modify":
            try:
                _modified(ctx.subject, outcome["modification"])
            except sp.PatchError:
                return deny(f"Hook {name} 的改写用不上（patch 与 subject 对不上）")
        return outcome

    async def _record(self, call: HookCall, records: list[tuple[str, dict[str, Any], str]]) -> None:
        if self.telemetry is None or not records:
            return
        try:
            async with self._bound(call) as telemetry:
                for span_name, attrs, event in records:
                    async with telemetry.start_span(span_name, attributes={"task_id": call.task_id, "round_seq": call.round_seq, **attrs}) as span:
                        if event:
                            span.add_event(event)
                await telemetry.flush()
        except Exception as err:  # noqa: BLE001 — 记录写不出去不影响 Hook 的结果（模块说明）
            log.warning("Policy Engine 的 %d 条记录没写进 Telemetry（%s）：task %s，%s", len(records), type(err).__name__, call.task_id, call.mount)

    @contextlib.asynccontextmanager
    async def _bound(self, call: HookCall) -> AsyncIterator[Telemetry]:
        assert self.telemetry is not None
        t = call.tenant
        yield self.telemetry.bind(TenantContext(tenant_id=t.tenant_id, user_id=t.user_id, workspace_id=t.workspace_id, session_id=t.session_id))

    def activities(self) -> dict[str, object]:
        return {names.RUN_HOOKS: self.run_hooks}
