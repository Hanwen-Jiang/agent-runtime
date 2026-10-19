"""租户绑定：校验与复制租户上下文；平台作用域必须写明用途。"""

from __future__ import annotations

import pytest

from agentruntime.common.v1 import tenant_pb2

from agentruntime_core.conformance.common import MALFORMED_TENANT_IDS
from agentruntime_core.errors import DeniedError, InvalidError
from agentruntime_core.tenancy import ScopeKind, platform_scope, tenant_scope

SESSION = "ses_01JA8Q2N4H6K8M0P2R4T6V8X0Z"


def full_context() -> tenant_pb2.TenantContext:
    return tenant_pb2.TenantContext(
        tenant_id="t1",
        user_id="u7",
        workspace_id="w3",
        session_id=SESSION,
        trace=tenant_pb2.TraceContext(traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"),
    )


def test_tenant_scope_copies_every_field():
    scope = tenant_scope(full_context())
    assert scope.kind is ScopeKind.TENANT and not scope.is_platform
    assert (scope.tenant_id, scope.user_id, scope.workspace_id, scope.session_id) == ("t1", "u7", "w3", SESSION)
    assert scope.traceparent.startswith("00-4bf92f35")


def test_scope_is_independent_of_the_original_message():
    context = full_context()
    scope = tenant_scope(context)
    context.tenant_id = "t2"
    assert scope.tenant_id == "t1"


def test_scope_converts_back_to_proto():
    context = full_context()
    assert tenant_scope(context).to_tenant_context() == context


def test_missing_context_is_denied():
    with pytest.raises(DeniedError) as info:
        tenant_scope(None)
    assert info.value.code == "missing_tenant"


def test_empty_tenant_id_is_denied():
    with pytest.raises(DeniedError):
        tenant_scope(tenant_pb2.TenantContext(user_id="u7"))


@pytest.mark.parametrize("bad_id", MALFORMED_TENANT_IDS)
def test_malformed_tenant_id_is_invalid(bad_id):
    with pytest.raises(InvalidError) as info:
        tenant_scope(tenant_pb2.TenantContext(tenant_id=bad_id))
    assert info.value.code == "malformed_tenant"


@pytest.mark.parametrize(
    "fields",
    [{"user_id": "U7"}, {"workspace_id": "../w"}, {"session_id": "ses_42"}],
    ids=["user", "workspace", "session"],
)
def test_malformed_optional_fields_are_invalid(fields):
    with pytest.raises(InvalidError):
        tenant_scope(tenant_pb2.TenantContext(tenant_id="t1", **fields))


def test_non_proto_context_is_invalid():
    with pytest.raises(InvalidError):
        tenant_scope({"tenant_id": "t1"})  # type: ignore[arg-type]


def test_platform_scope_requires_purpose():
    scope = platform_scope("  registry defaults ")
    assert scope.is_platform and scope.purpose == "registry defaults" and scope.tenant_id == ""
    for purpose in ("", "   "):
        with pytest.raises(InvalidError):
            platform_scope(purpose)


def test_platform_scope_has_no_tenant_context():
    with pytest.raises(DeniedError):
        platform_scope("x").to_tenant_context()
