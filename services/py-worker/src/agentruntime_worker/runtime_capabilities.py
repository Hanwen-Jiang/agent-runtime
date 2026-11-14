"""Current task authority for model context, read from grants and the tool catalog.

This is a status read, never a permission request or a claim of a successful network
probe. It runs inside activities, not workflow code, and refreshes before each answer.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from agentruntime_worker.sandbox.grants import GrantStore

_EFFECTS = ('none', 'read', 'write_reversible', 'write_irreversible')


class RuntimeCapabilities:
    def __init__(self, grants: GrantStore | None, tools: Any) -> None:
        self.grants, self.tools = grants, tools

    async def load(self, tenant: Any, task_id: str, *, ceiling: str = 'read', tool_names: list[str] | None = None) -> dict[str, Any]:
        snapshot: dict[str, Any] = {'source': 'runtime_task_state', 'task_id': task_id,
            'checked_at': datetime.now(UTC).isoformat(), 'network': {'authorization': 'unknown', 'grants': []},
            'network_probe': 'not_performed', 'tools': [], 'tools_status': 'unknown', 'effect_ceiling': ceiling}
        if not tenant.tenant_id or not task_id:
            return snapshot
        try:
            effects = await asyncio.wait_for(self.tools.all_effects(tenant, task_id), timeout=2)
            max_effect = _EFFECTS.index(ceiling) if ceiling in _EFFECTS else 0
            snapshot['tools'] = sorted(name for name, effect in effects.items()
                if effect in _EFFECTS and _EFFECTS.index(effect) <= max_effect and (tool_names is None or name in tool_names))[:256]
            snapshot['tools_status'] = 'ready'
        except Exception:
            pass  # A failed observation is unknown, never a claim that tools do not exist.
        if self.grants is not None:
            try:
                grants = await asyncio.wait_for(self.grants.for_task(tenant.tenant_id, task_id, active_only=True), timeout=2)
                public = any(g.scope == 'public' for g in grants)
                snapshot['network'] = {'authorization': 'public' if public else 'hosts' if grants else 'none',
                    'grants': [{'scope': g.scope, 'hosts': list(g.hosts), 'ports': list(g.ports), 'expires_at': g.expires_at} for g in grants]}
            except Exception:
                pass
        return snapshot


def context_text(snapshot: dict[str, Any] | None) -> str:
    if not snapshot:
        return ''
    return ('当前任务的运行时状态（服务端即时读取，优先于历史对话里对权限或工具的旧描述）：\n'
            + json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            + '\nnetwork.authorization=public/hosts 表示本任务已有有效授权，不要再说完全不能联网或重复索要同一授权；'
            'none 表示当前任务没有有效沙箱联网授权，unknown 表示状态未读取成功。工具仅以当前 tools 为准。'
            '当前 tools 中包含 shell_run 时，可在授权范围内通过代理访问公网；没有独立浏览器/搜索工具不等于无法联网。'
            '授权状态不是访问成功的证据；用户要求查询、检索或验证网站连通性时必须执行可用工具后回答，不能仅凭历史或此快照声称已访问。')


class RuntimeCapabilityContextSource:
    def __init__(self, inner: Any, capabilities: RuntimeCapabilities) -> None:
        self.inner, self.capabilities = inner, capabilities

    async def load(self, inp: Any) -> Any:
        ctx = await self.inner.load(inp)
        snapshot = await self.capabilities.load(inp.tenant, inp.task_id, ceiling=ctx.effect_ceiling)
        return replace(ctx, runtime_capabilities=snapshot)


def from_config(config: Any, tools: Any = None) -> RuntimeCapabilities:
    from agentruntime_worker.tools import ToolCatalog, dev_tools, mcp_leases, _relational
    grants = GrantStore(_relational(config)) if config.sandbox_service_address else None
    return RuntimeCapabilities(grants, tools or ToolCatalog(dev_tools(config), mcp_leases(config)))
