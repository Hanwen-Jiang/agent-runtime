"""S-03 验收中的租户边界（CLAUDE.md 租户隔离）：租户由服务端根据认证身份确定，请求里的 tenant_id
只用来校验一致；访问别的租户的 Session 或 Task 按不存在处理。"""

from __future__ import annotations

import pytest

from e2e_support.api import ApiError, SseStream, TaskApi, stream_until_finished
from e2e_support.stack import Stack

pytestmark = pytest.mark.e2e


def expect(code: str, fn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ApiError) as err:
        fn()
    assert err.value.code == code, err.value


def test_unauthenticated_requests_are_rejected(stack: Stack) -> None:
    anonymous = TaskApi(stack.http_url, token=None)
    expect("unauthenticated", lambda: anonymous.submit())
    forged = TaskApi(stack.http_url, token="eyJ0IjoidDEifQ.forged")
    expect("unauthenticated", lambda: forged.submit())


def test_request_tenant_must_match_the_identity(api: TaskApi) -> None:
    expect("permission_denied", lambda: api.submit(tenant_id="t2"))
    api.submit(tenant_id="t1")


def test_other_tenants_cannot_see_or_touch_a_task(stack: Stack, api: TaskApi, other_tenant: TaskApi) -> None:
    sub = api.submit()
    stream_until_finished(sub["eventsUrl"], api.token or "")
    expect("not_found", lambda: other_tenant.query(sub["taskId"]))
    expect("not_found", lambda: other_tenant.cancel(sub["taskId"]))
    expect("not_found", lambda: other_tenant.reply(sub["taskId"], "confirm", "approve"))
    expect("not_found", lambda: other_tenant.submit(session_id=sub["sessionId"]))
    stream = SseStream(sub["eventsUrl"], other_tenant.token)
    try:
        assert stream.status == 404
    finally:
        stream.close()
