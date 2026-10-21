"""认证身份（CLAUDE.md“租户由服务端根据认证身份确定；请求里的 tenant_id 只用来校验一致”；计划 S-12b）。

开发实现：令牌与 task-service 的开发令牌同一格式（libs/go-core/dev/localingress）：
base64url(JSON 载荷).base64url(HMAC-SHA256)，载荷 {"t": 租户, "u": 用户, "exp": 过期的 Unix 秒}；
`task-service dev-token` 签出的令牌可以直接用。Worker 调用时用同一把签名密钥按 Task 的租户签短期令牌，
载荷另可带 w（workspace）、s（session）与 r（角色列表）；带了就要与请求一致。
生产环境换成 mTLS 加签名的租户断言（另行实现）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from agentruntime_core.clock import Clock, SystemClock
from agentruntime_core.errors import DeniedError
from agentruntime_core.tenancy import TenantContext

ROLES = ("member", "maintainer", "admin", "platform_reviewer")
"""maintainer 管 Workspace 的经验库，admin 管租户级记忆与回滚，platform_reviewer 审核晋升到平台的经验。"""

_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_SESSION = re.compile(r"ses_[0-7][0-9A-HJKMNP-TV-Z]{25}")


@dataclass(frozen=True)
class Identity:
    tenant_id: str
    user_id: str = ""
    workspace_id: str = ""
    session_id: str = ""
    roles: frozenset[str] = field(default_factory=frozenset)

    def tenant_context(self) -> TenantContext:
        return TenantContext(tenant_id=self.tenant_id, user_id=self.user_id, workspace_id=self.workspace_id, session_id=self.session_id)


class Authenticator(Protocol):
    def authenticate(self, token: str) -> Identity:
        """校验令牌，返回身份；令牌缺失、格式不对、签名不对或过期：DeniedError（code unauthenticated）。"""
        ...


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _unauthenticated(message: str) -> DeniedError:
    return DeniedError("unauthenticated", message)


class DevTokenAuthenticator:
    def __init__(self, signing_key: bytes, *, clock: Clock | None = None) -> None:
        self.signing_key = signing_key
        self.clock = clock or SystemClock()

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self.signing_key, payload.encode(), hashlib.sha256).digest())

    def authenticate(self, token: str) -> Identity:
        payload, _, sig = (token or "").partition(".")
        if not payload or not sig:
            raise _unauthenticated("缺少或无法识别的凭据")
        if not hmac.compare_digest(sig.encode(), self._sign(payload).encode()):
            raise _unauthenticated("凭据签名不对")
        try:
            claims = json.loads(_unb64(payload))
        except (binascii.Error, ValueError):
            raise _unauthenticated("凭据格式不对") from None
        if not isinstance(claims, dict) or not isinstance(claims.get("t"), str) or not _SLUG.fullmatch(claims["t"]):
            raise _unauthenticated("凭据内容不对")
        user, workspace, session = claims.get("u", ""), claims.get("w", ""), claims.get("s", "")
        roles = claims.get("r", [])
        if (
            not all(isinstance(v, str) for v in (user, workspace, session))
            or any(v and not _SLUG.fullmatch(v) for v in (user, workspace))
            or (session and not _SESSION.fullmatch(session))
            or not isinstance(roles, list)
            or not set(roles) <= set(ROLES)
        ):
            raise _unauthenticated("凭据内容不对")
        expires = claims.get("exp")
        if not isinstance(expires, int) or expires <= self.clock.now().timestamp():
            raise _unauthenticated("凭据已过期")
        return Identity(tenant_id=claims["t"], user_id=user, workspace_id=workspace, session_id=session, roles=frozenset(roles))


def issue_dev_token(
    signing_key: bytes,
    *,
    tenant: str,
    user: str,
    ttl: timedelta = timedelta(minutes=5),
    workspace: str = "",
    session: str = "",
    roles: Iterable[str] = (),
    now: datetime | None = None,
) -> str:
    claims: dict[str, object] = {"t": tenant, "u": user, "exp": int(((now or datetime.now(UTC)) + ttl).timestamp())}
    if workspace:
        claims["w"] = workspace
    if session:
        claims["s"] = session
    if roles:
        claims["r"] = sorted(set(roles))
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return payload + "." + _b64(hmac.new(signing_key, payload.encode(), hashlib.sha256).digest())


def bind_request(identity: Identity, tenant: TenantContext | None) -> Identity:
    """核对请求里的租户上下文与认证身份一致，返回本次请求的身份（补上令牌没带、请求带了的 workspace 与 session）。
    缺少租户上下文或租户、用户与身份不一致：DeniedError。"""
    if tenant is None or not tenant.tenant_id:
        raise DeniedError("missing_tenant", "请求缺少租户上下文")
    if tenant.tenant_id != identity.tenant_id or tenant.user_id != identity.user_id:
        raise DeniedError("tenant_mismatch", "请求的租户上下文与认证身份不一致")
    bound = identity
    for attr, requested in (("workspace_id", tenant.workspace_id), ("session_id", tenant.session_id)):
        claimed = getattr(identity, attr)
        if claimed and requested and claimed != requested:
            raise DeniedError("tenant_mismatch", "请求的租户上下文与认证身份不一致")
        if requested and not claimed:
            bound = replace(bound, **{attr: requested})
    return bound
