"""ToolDriver（设计书 4.9）：工具登记与调用。

只在 Python 侧（Node Executor 的 program 节点）使用。登记内容：名称、版本、输入输出的 JSON Schema、effect、
权限、超时与是否支持按幂等键查询。内核只认这份强类型契约；MCP 工具由边缘的 MCP Gateway 登记并补齐 effect 等字段。
登记属于运维操作，由实现各自提供，不在本接口内。

语义：
- 未登记的工具不能调用：not_found。租户专属的工具只对该租户可见；平台工具对所有租户可见。
- 版本必须写明，形如 v3（与 Task 固定的版本一致）。
- 参数不符合 input_schema：invalid。工具返回不符合 output_schema：invalid（code = invalid_output）。
- 工具要求的权限没有授予本作用域：denied。
- effect 为 write_reversible 或 write_irreversible 的工具必须带幂等键，否则 invalid。
- 同一作用域、同一工具、同一幂等键：只真正执行一次；再次调用返回第一次的结果，replayed=True。
  同一幂等键配不同参数：conflict。幂等记录按作用域隔离。
- 超时：transient（code = timeout）。写操作超时后结果未知，由 Node Executor 先用 lookup 对账（附录 K.4）。
- lookup 按幂等键查询写操作是否已经生效：已生效返回结果，没有返回 None；工具不支持查询：invalid（code = lookup_unsupported）。
- 工具执行时拿到的租户上下文来自客户端的作用域，参数无法改变它。task_id 由调用方（Node Executor）给出，只用来归属，
  不参与幂等（同一幂等键的再次调用即使 task_id 不同也返回第一次的结果）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from agentruntime_core.enums import Effect
from agentruntime_core.providers.base import Bindable
from agentruntime_core.providers.secret_store import Credential


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    effect: Effect
    permissions: frozenset[str]
    timeout: timedelta
    supports_idempotency_lookup: bool = False


@dataclass(frozen=True)
class ToolInvocation:
    """工具实现收到的一次调用。tenant_id 来自客户端的作用域（平台作用域为空串），不来自参数。"""

    tenant_id: str
    arguments: Mapping[str, Any]
    idempotency_key: str | None
    credential: Credential | None
    # 调用者的 workspace（来自客户端的作用域，没有为空串），参数改变不了它；平台内工具按它划分工作区（T-10）。
    workspace_id: str = ""
    # 调用者的会话与用户（同样来自作用域），会话级沙箱按会话复用环境（D-13）。
    session_id: str = ""
    user_id: str = ""
    # 发起调用的 Task（由 Node Executor 给出，不来自参数）；联网授权属于 Task（D-16）。
    task_id: str = ""


ToolHandler = Callable[[ToolInvocation], Awaitable[Any]]


@dataclass(frozen=True)
class ToolResult:
    output: Any
    idempotency_key: str | None
    # 命中了幂等记录，没有真正执行。
    replayed: bool


class ToolDriver(Protocol):
    async def list_tools(self) -> list[ToolSpec]: ...

    async def describe(self, name: str, version: str) -> ToolSpec: ...

    async def invoke(
        self,
        name: str,
        version: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        credential: Credential | None = None,
        task_id: str = "",
    ) -> ToolResult: ...

    async def lookup(self, name: str, version: str, idempotency_key: str) -> ToolResult | None: ...


class ToolDriverProvider(Bindable[ToolDriver], Protocol):
    pass
