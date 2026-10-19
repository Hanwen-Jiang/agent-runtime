"""租户绑定（计划 S-02 难点“租户上下文的传法”：绑定在按请求创建的客户端上，全仓统一）。

Python 与 Go 共用（Go 侧见 libs/go-core/tenancy），两边语义一致：

- 每个 Provider 都是一个长期存在的工厂，按请求调用 bind(tenant) 得到绑定了租户的客户端；
  之后在客户端上的调用不再传租户，也无法改成别的租户。
- bind 时复制租户上下文；调用方事后修改原对象，不影响已绑定的客户端。
- 缺少租户上下文、tenant_id 为空：denied。tenant_id 等字段格式不对：invalid。
- 平台自有的数据（基础设施密钥、平台级经验、平台级缓存计数等）用 bind_platform(purpose) 访问，
  purpose 写明用途，必填。平台作用域与任何租户作用域互不可见。
- 跨租户读取（例如 Learning Pipeline）不在本期接口内，由 S-12 用单独的角色并留审计。
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

from agentruntime.common.v1 import tenant_pb2

from agentruntime_core.errors import DeniedError, InvalidError

TenantContext = tenant_pb2.TenantContext

# 与 /schemas/defs.schema.json 的 tenant_id、workspace_id、user_id 一致。
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
# 与 proto IdKind 的 ses 前缀一致。
_SESSION_ID = re.compile(r"^ses_[0-7][0-9A-HJKMNP-TV-Z]{25}$")


class ScopeKind(enum.Enum):
    TENANT = "tenant"
    PLATFORM = "platform"


@dataclass(frozen=True)
class Scope:
    """一个已绑定客户端的作用域。由 tenant_scope() 或 platform_scope() 构造，实现据此隔离数据。"""

    kind: ScopeKind
    tenant_id: str = ""
    user_id: str = ""
    workspace_id: str = ""
    session_id: str = ""
    traceparent: str = ""
    tracestate: str = ""
    # 平台作用域的用途说明。
    purpose: str = ""

    @property
    def is_platform(self) -> bool:
        return self.kind is ScopeKind.PLATFORM

    def to_tenant_context(self) -> TenantContext:
        """转回 proto，供跨服务调用携带。平台作用域没有租户上下文。"""
        if self.is_platform:
            raise DeniedError("platform_scope", "平台作用域没有租户上下文")
        return TenantContext(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            session_id=self.session_id,
            trace=tenant_pb2.TraceContext(traceparent=self.traceparent, tracestate=self.tracestate),
        )


def tenant_scope(tenant: TenantContext | None) -> Scope:
    """校验并复制租户上下文。所有 Provider 实现的 bind() 都调用它。"""
    if tenant is None:
        raise DeniedError("missing_tenant", "缺少租户上下文")
    if not isinstance(tenant, TenantContext):
        raise InvalidError("malformed_tenant", "租户上下文必须是 agentruntime.common.v1.TenantContext")
    if not tenant.tenant_id:
        raise DeniedError("missing_tenant", "租户上下文缺少 tenant_id")
    # 用 fullmatch：re 的 $ 会匹配结尾换行符之前的位置，"t1\n" 会被 match 放过。
    if not _SLUG.fullmatch(tenant.tenant_id):
        raise InvalidError("malformed_tenant", "tenant_id 格式不对")
    for name in ("user_id", "workspace_id"):
        value = getattr(tenant, name)
        if value and not _SLUG.fullmatch(value):
            raise InvalidError("malformed_tenant", f"{name} 格式不对")
    if tenant.session_id and not _SESSION_ID.fullmatch(tenant.session_id):
        raise InvalidError("malformed_tenant", "session_id 格式不对")
    return Scope(
        kind=ScopeKind.TENANT,
        tenant_id=tenant.tenant_id,
        user_id=tenant.user_id,
        workspace_id=tenant.workspace_id,
        session_id=tenant.session_id,
        traceparent=tenant.trace.traceparent,
        tracestate=tenant.trace.tracestate,
    )


def platform_scope(purpose: str) -> Scope:
    """平台作用域。所有 Provider 实现的 bind_platform() 都调用它。"""
    if not isinstance(purpose, str) or not purpose.strip():
        raise InvalidError("missing_purpose", "平台作用域必须写明用途")
    return Scope(kind=ScopeKind.PLATFORM, purpose=purpose.strip())
