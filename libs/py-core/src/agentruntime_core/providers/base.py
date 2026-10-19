"""所有 Provider 工厂共用的绑定协议。"""

from __future__ import annotations

from typing import Protocol, TypeVar

from agentruntime_core.tenancy import TenantContext

ClientT = TypeVar("ClientT", covariant=True)


class Bindable(Protocol[ClientT]):
    def bind(self, tenant: TenantContext) -> ClientT:
        """绑定租户，得到客户端。缺少租户上下文：denied；格式不对：invalid。不做 I/O。"""
        ...

    def bind_platform(self, purpose: str) -> ClientT:
        """绑定平台作用域，purpose 必填。平台作用域与任何租户作用域互不可见。"""
        ...

    async def aclose(self) -> None:
        """释放连接池等资源。"""
        ...
