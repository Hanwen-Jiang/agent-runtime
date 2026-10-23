"""Policy Engine（设计书 3.2.6、附录 F；T-09，细化见 docs/plan/policy-engine.md）。

Worker 进程内的库：PolicyEngine.run_hooks 是 Stage Runner 与 Task 工作流调用的 local activity（names.RUN_HOOKS），
Worker 默认就用它（代替 S-04 的桩）。规则：

- 按顺序执行 HookCall.hooks，每个 Hook 只返回 allow、deny、modify、inject 之一（hook_result.schema.json）；
  遇到第一个 deny 就停，后面的不执行。
- fail-closed：名字不认识、挂错挂载点、Hook 抛异常，一律按 deny；异常只记类型，不记原文（可能带数据）。
- 每次 deny 写一条审计记录（policy.decision）；trace_result、checkpoint、audit_log 的记录也由这里写进 Telemetry。
  记录里只有 Node、模板、effect、哈希与理由，不含输入与结果的原文（trace_result 写结果原文是附录 D.4 的要求）。
- Hook 是纯函数（hooks.py），不调模型、不做网络 I/O；Telemetry 只在引擎里用。
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterator, Mapping
from typing import Any

from temporalio import activity

from agentruntime_core.providers.telemetry import Telemetry, TelemetryProvider
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker import names
from agentruntime_worker.activities.types import HookCall
from agentruntime_worker.policy.hooks import HOOKS, HookContext, HookSpec, deny

__all__ = ["HOOKS", "HookContext", "HookSpec", "PolicyEngine"]

# 与 defs.schema.json 的 hook_name 一致；不合格的名字在结果里写成 invalid_hook_name。
_HOOK_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class PolicyEngine:
    """hooks 默认是 HOOKS（hooks.py）；telemetry 为 None 时不留记录。"""

    def __init__(self, *, telemetry: TelemetryProvider | None = None, hooks: Mapping[str, HookSpec] | None = None) -> None:
        self.telemetry = telemetry
        self.hooks = dict(HOOKS if hooks is None else hooks)

    @activity.defn(name=names.RUN_HOOKS)
    async def run_hooks(self, call: HookCall) -> list[dict[str, Any]]:
        ctx = HookContext(tenant=call.tenant, task_id=call.task_id, round_seq=call.round_seq, mount=call.mount, subject=call.subject)
        results: list[dict[str, Any]] = []
        records: list[tuple[str, dict[str, Any], str]] = []
        for name in call.hooks:
            outcome = self._evaluate(name, ctx)
            record = outcome.pop("_record", None)
            if record is not None:
                records.append(record)
            result = {"hook": name if _HOOK_NAME.match(name) else "invalid_hook_name", "mount": call.mount, **outcome}
            results.append(result)
            if result["decision"] == "deny":
                node = ctx.subject.get("node") or {}
                attrs = {"hook": result["hook"], "mount": call.mount, "decision": "deny", "reason": result["reason"], "node_id": node.get("id", "")}
                records.append(("policy.decision", attrs, ""))
                break
        await self._record(call, records)
        return results

    def _evaluate(self, name: str, ctx: HookContext) -> dict[str, Any]:
        spec = self.hooks.get(name)
        if spec is None:
            return deny(f"不认识的 Hook {name}")
        if ctx.mount not in spec.mounts:
            return deny(f"Hook {name} 不能挂在 {ctx.mount}")
        try:
            return dict(spec.run(ctx))
        except Exception as err:  # noqa: BLE001 — Hook 出错按拒绝处理（fail-closed）
            return deny(f"Hook {name} 执行出错（{type(err).__name__}）")

    async def _record(self, call: HookCall, records: list[tuple[str, dict[str, Any], str]]) -> None:
        if self.telemetry is None or not records:
            return
        async with self._bound(call) as telemetry:
            for span_name, attrs, event in records:
                async with telemetry.start_span(span_name, attributes={"task_id": call.task_id, "round_seq": call.round_seq, **attrs}) as span:
                    if event:
                        span.add_event(event)
            await telemetry.flush()

    @contextlib.asynccontextmanager
    async def _bound(self, call: HookCall) -> AsyncIterator[Telemetry]:
        assert self.telemetry is not None
        t = call.tenant
        yield self.telemetry.bind(TenantContext(tenant_id=t.tenant_id, user_id=t.user_id, workspace_id=t.workspace_id, session_id=t.session_id))

    def activities(self) -> dict[str, object]:
        return {names.RUN_HOOKS: self.run_hooks}
