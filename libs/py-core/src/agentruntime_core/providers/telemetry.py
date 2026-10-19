"""Telemetry（设计书 4.13、附录 J.1）：span、指标与日志的打点与导出。

两种语言共用：Go 侧见 libs/go-core/providers/telemetry，方法一一对应，语义一致。

语义：
- 绑定租户的客户端自动给每个 span、指标、日志加上 tenant_id、user_id、workspace_id、session_id；
  调用方传入同名属性一律忽略，无法伪造租户。平台作用域加 scope=platform，不带租户属性。
- 租户上下文带有 traceparent 时，根 span 接在它下面（trace_id 相同）。
- 值为 Secret 的属性、键名含 password、secret、token、api_key、apikey、authorization、credential、cookie 的属性，
  一律记为 [REDACTED]。日志消息中的 Secret 同样只显示掩码。
- start_span 内抛出的异常：span 记为 error 并记录异常类型，异常照常抛出。
- 导出失败不影响业务调用：start_span、count、observe、log 只在参数不合法（例如名称为空）时报 invalid；
  导出失败只体现在 flush，且只能是 transient。
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from agentruntime_core.providers.base import Bindable
from agentruntime_core.secrets import Secret

REDACTED = "[REDACTED]"

AttributeValue = str | bool | int | float | Secret | Sequence[str] | Sequence[int] | Sequence[float]
Attributes = Mapping[str, AttributeValue]


class LogLevel(enum.StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Span(Protocol):
    @property
    def trace_id(self) -> str: ...

    @property
    def span_id(self) -> str: ...

    def set_attributes(self, attributes: Attributes) -> None: ...

    def add_event(self, name: str, attributes: Attributes | None = None) -> None: ...

    def record_error(self, error: BaseException) -> None: ...


class Telemetry(Protocol):
    def start_span(self, name: str, *, attributes: Attributes | None = None) -> AbstractAsyncContextManager[Span]: ...

    async def count(self, name: str, value: int = 1, *, attributes: Attributes | None = None) -> None: ...

    async def observe(self, name: str, value: float, *, attributes: Attributes | None = None) -> None: ...

    async def log(self, level: LogLevel, message: str, *, attributes: Attributes | None = None) -> None: ...

    async def flush(self) -> None: ...


class TelemetryProvider(Bindable[Telemetry], Protocol):
    pass
