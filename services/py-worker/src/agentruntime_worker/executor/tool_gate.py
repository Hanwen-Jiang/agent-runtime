"""工具调用的审批兜底（计划 S-05“审批双保险”；D16）。

tool_loop 里调用 write_irreversible 的工具前，Stage Runner 先问人；执行工具调用的 activity 再查一次：
没有这次调用自己的、通过的审批记录（审批 id 是 <调用 id>.1）就拒绝执行。所有 call_tool 的实现都先调这里。
"""

from __future__ import annotations

from temporalio.exceptions import ApplicationError

from agentruntime_worker.activities.types import ToolCallInput
from agentruntime_worker.executor.node_executor import DENIED


def require_tool_gate(inp: ToolCallInput) -> None:
    """effect 为 write_irreversible 时，要求带着这次调用自己的、通过的审批记录；否则抛不可重试的 denied。"""
    if inp.effect != "write_irreversible" and inp.tool != "request_network":
        return
    gate = inp.gate
    if gate is None or gate.option != "approve" or gate.gate_node != f"{inp.node_id}.1" or (inp.tool == "request_network" and not gate.approved_by):
        raise ApplicationError(f"不可逆工具 {inp.tool} 的调用 {inp.node_id} 没有通过的人工审批记录", type=DENIED, non_retryable=True)
