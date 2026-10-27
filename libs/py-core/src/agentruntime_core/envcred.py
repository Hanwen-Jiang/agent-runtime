"""沙箱环境的短期凭据（docs/plan/sandbox-platform.md 第 3 节；D-15 包镜像、D-16 出网网关）。

Worker 签发、Go 服务（services/go-pkg-mirror、services/go-egress-gateway）校验，不查表就能知道是哪个租户的哪个环境：

    ar1.<base64url(JSON)>.<base64url(HMAC-SHA256(key, "ar1." + base64url(JSON)))>
    JSON：{"e": env_ref, "t": tenant_id, "w": workspace_id, "s": session_id, "x": 到期的 Unix 秒, "k": 用途, "q": {限额}}

- 用途 k 只能是 mirror（包镜像）或 egress（出网网关）：一个凭据只能用在一处。q 是这个环境带去的限额（例如包镜像的
  每天未命中字节数，取自租户策略），由签发方填，校验方照用，请求里写的不作数。
- 密钥取自 SecretStore 平台作用域的 sandbox/env-credential-key（部署共用，至少 32 字节）。
- 校验：前缀、签名（常数时间比较）、JSON、用途、未到期、租户 ID 的格式；任何一项不对都是同一个错误，不说明是哪一项。
- 与 Go 的 libs/go-core/envcred 对着同一组向量测试（tests/contract/sandbox_credential_vectors.json）。
- 凭据不进日志、trace 与错误信息；记录时只写环境号。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from agentruntime_core.errors import DeniedError, InvalidError

PREFIX = "ar1."
PURPOSES = ("mirror", "egress")
SECRET_NAME = "sandbox/env-credential-key"
_TENANT = re.compile(r"^(_platform|[a-z0-9][a-z0-9_-]{0,62})$")
_ENV_REF = re.compile(r"^sbe-[0-9a-f]{16}$")


@dataclass(frozen=True)
class Claims:
    env_ref: str
    tenant_id: str
    expires_at: int
    purpose: str
    workspace_id: str = ""
    session_id: str = ""
    limits: Mapping[str, int] = field(default_factory=dict)

    def payload(self) -> dict[str, object]:
        out: dict[str, object] = {"e": self.env_ref, "t": self.tenant_id, "x": self.expires_at, "k": self.purpose}
        if self.workspace_id:
            out["w"] = self.workspace_id
        if self.session_id:
            out["s"] = self.session_id
        if self.limits:
            out["q"] = dict(self.limits)
        return out


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(key: bytes, body: str) -> str:
    return _b64(hmac.new(key, (PREFIX + body).encode(), hashlib.sha256).digest())


def issue(key: bytes, claims: Claims) -> str:
    if len(key) < 32:
        raise InvalidError("weak_credential_key", "环境凭据的密钥至少 32 字节")
    if claims.purpose not in PURPOSES:
        raise InvalidError("invalid_purpose", "环境凭据的用途只能是 mirror 或 egress")
    if not _TENANT.match(claims.tenant_id) or not _ENV_REF.match(claims.env_ref):
        raise InvalidError("invalid_claims", "环境凭据的租户或环境号格式不对")
    body = _b64(json.dumps(claims.payload(), separators=(",", ":"), sort_keys=True).encode())
    return f"{PREFIX}{body}.{_signature(key, body)}"


def verify(key: bytes, token: str, *, purpose: str, now: datetime) -> Claims:
    """校验并返回凭据里的内容；不对一律 denied（invalid_credential），不说明是哪一项。"""
    bad = DeniedError("invalid_credential", "环境凭据无效或已过期")
    if not isinstance(token, str) or not token.startswith(PREFIX) or token.count(".") != 2:
        raise bad
    body, sig = token[len(PREFIX) :].split(".", 1)
    if not hmac.compare_digest(_signature(key, body), sig):
        raise bad
    try:
        data = json.loads(_unb64(body))
    except ValueError:
        raise bad from None
    if not isinstance(data, dict):
        raise bad
    try:
        claims = Claims(
            env_ref=str(data["e"]), tenant_id=str(data["t"]), expires_at=int(data["x"]), purpose=str(data["k"]),
            workspace_id=str(data.get("w") or ""), session_id=str(data.get("s") or ""),
            limits={str(k): int(v) for k, v in dict(data.get("q") or {}).items()},
        )
    except (KeyError, TypeError, ValueError):
        raise bad from None
    if claims.purpose != purpose or claims.expires_at <= int(now.timestamp()) or not _TENANT.match(claims.tenant_id):
        raise bad
    return claims


__all__ = ["PREFIX", "PURPOSES", "SECRET_NAME", "Claims", "issue", "verify"]
