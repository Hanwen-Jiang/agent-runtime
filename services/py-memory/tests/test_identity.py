"""S-12b：租户由服务端根据认证身份确定，请求里的租户上下文只用来校验一致（CLAUDE.md 租户隔离）。

开发令牌与 task-service 的开发令牌同一格式（libs/go-core/dev/localingress）：base64url(JSON).base64url(HMAC-SHA256)，
载荷的键是 t（租户）、u（用户）、exp。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from agentruntime_core.errors import DeniedError
from agentruntime_core.tenancy import TenantContext
from agentruntime_core.testing import FakeClock

from agentruntime_memory.identity import DevTokenAuthenticator, Identity, bind_request, issue_dev_token

KEY = b"dev-signing-key-for-tests"
NOW = datetime(2026, 10, 1, tzinfo=UTC)


def go_style_token(payload: dict, key: bytes = KEY) -> str:
    """按 localingress 的格式手工签一个令牌（不经被测代码）。"""
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(hmac.new(key, body.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return f"{body}.{sig}"


def auth() -> DevTokenAuthenticator:
    return DevTokenAuthenticator(KEY, clock=FakeClock(NOW))


def test_task_service_dev_tokens_are_accepted() -> None:
    token = go_style_token({"t": "t-alpha", "u": "u-alice", "exp": int((NOW + timedelta(hours=1)).timestamp())})
    identity = auth().authenticate(token)
    assert identity == Identity(tenant_id="t-alpha", user_id="u-alice")


def test_issued_tokens_carry_workspace_session_and_roles() -> None:
    token = issue_dev_token(KEY, tenant="t-alpha", user="u-alice", workspace="w-1", session="ses_01J" + "0" * 22 + "A", roles=["maintainer"], now=NOW)
    identity = auth().authenticate(token)
    assert identity.workspace_id == "w-1" and identity.session_id.startswith("ses_") and identity.roles == frozenset({"maintainer"})


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-token",
        go_style_token({"t": "t-alpha", "u": "u-alice", "exp": int((NOW + timedelta(hours=1)).timestamp())}, key=b"other-key"),
        go_style_token({"t": "t-alpha", "u": "u-alice", "exp": int((NOW - timedelta(seconds=1)).timestamp())}),
        go_style_token({"u": "u-alice", "exp": int((NOW + timedelta(hours=1)).timestamp())}),
        go_style_token({"t": "T ALPHA", "u": "u-alice", "exp": int((NOW + timedelta(hours=1)).timestamp())}),
    ],
    ids=["empty", "malformed", "bad_signature", "expired", "no_tenant", "bad_tenant"],
)
def test_bad_tokens_are_unauthenticated(token: str) -> None:
    with pytest.raises(DeniedError) as err:
        auth().authenticate(token)
    assert err.value.code == "unauthenticated"


def test_the_request_tenant_must_match_the_identity() -> None:
    alice = Identity(tenant_id="t-alpha", user_id="u-alice")
    bound = bind_request(alice, TenantContext(tenant_id="t-alpha", user_id="u-alice", workspace_id="w-1"))
    assert bound.workspace_id == "w-1", "令牌没带的 workspace 取自请求"
    for bad in (None, TenantContext(), TenantContext(tenant_id="t-beta", user_id="u-alice"), TenantContext(tenant_id="t-alpha", user_id="u-bob")):
        with pytest.raises(DeniedError):
            bind_request(alice, bad)


def test_claims_in_the_token_cannot_be_overridden_by_the_request() -> None:
    alice = Identity(tenant_id="t-alpha", user_id="u-alice", workspace_id="w-1")
    with pytest.raises(DeniedError):
        bind_request(alice, TenantContext(tenant_id="t-alpha", user_id="u-alice", workspace_id="w-2"))
