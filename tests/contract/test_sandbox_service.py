"""SBX-01：Sandbox Service 的契约（docs/plan/sbx-01-sandbox-service-2026-11-03.md 第 4 节）。

- 每个请求的字段 1 是 TenantContext（流式导入的是第一条消息的 header），服务据此绑定所有 Provider。
- 响应里没有凭据：平台注入的环境变量（包镜像、出网代理、测试用密钥）只在服务里，Lease 只给测试用密钥的名字。
- task-service 能调的只读方法都在服务上，且不带 lease（不借环境）。
- 不按租户灰度（2026-11-04 用户纠正）：租户策略里没有经不经服务的开关，整个部署一起切。
"""

from __future__ import annotations

import json
from pathlib import Path

from agentruntime.sandbox.v1 import sandbox_pb2 as sb

ROOT = Path(__file__).resolve().parents[2]
TENANT_CONTEXT = "agentruntime.common.v1.TenantContext"
READ_ONLY = {"ListFiles", "ReadFile", "Grep", "GetDiff", "GetLog", "DescribeTenant"}
SECRET_WORDS = ("secret", "token", "credential", "password", "proxy")


def _service():
    return sb.DESCRIPTOR.services_by_name["SandboxService"]


def test_every_request_carries_the_tenant_as_field_one() -> None:
    for method in _service().methods:
        message = method.input_type
        if method.client_streaming:
            header = message.fields_by_name["header"].message_type
            assert header.fields_by_name["tenant"].number == 1, method.name
            assert header.fields_by_name["tenant"].message_type.full_name == TENANT_CONTEXT
            continue
        field = message.fields_by_number[1]
        assert field.name == "tenant" and field.message_type.full_name == TENANT_CONTEXT, method.name


def test_read_only_methods_need_no_lease() -> None:
    names = {m.name for m in _service().methods}
    assert READ_ONLY <= names
    for method in _service().methods:
        if method.name in READ_ONLY:
            assert "lease_id" not in method.input_type.fields_by_name, method.name


def test_lease_methods_address_the_lease_by_id() -> None:
    lease_methods = {"Exec", "WriteFile", "ReadLeaseFile", "ListLeaseDir", "GrepLease", "Manifest", "ExportArchive", "PreviewAddress",
                     "Release", "Snapshot", "Discard"}
    for method in _service().methods:
        if method.name in lease_methods:
            assert "lease_id" in method.input_type.fields_by_name, method.name


def test_responses_carry_no_credentials() -> None:
    seen: set[str] = set()

    def walk(message) -> None:
        if message.full_name in seen or not message.full_name.startswith("agentruntime.sandbox."):
            return
        seen.add(message.full_name)
        for field in message.fields:
            lowered = field.name.lower()
            assert lowered != "env" and not any(w in lowered for w in SECRET_WORDS), f"{message.full_name}.{field.name}"
            if field.message_type is not None:
                walk(field.message_type)

    for method in _service().methods:
        walk(method.output_type)
    lease = sb.Lease.DESCRIPTOR.fields_by_name
    assert lease["lease_id"].number == 1 and "test_env_names" in lease and "env" not in lease


def test_exec_env_is_only_on_the_request() -> None:
    assert "env" in sb.ExecRequest.DESCRIPTOR.fields_by_name
    assert "env" not in sb.ExecResponse.DESCRIPTOR.fields_by_name


def test_there_is_no_per_tenant_switch() -> None:
    schema = json.loads((ROOT / "schemas" / "tenant_policy.schema.json").read_text("utf-8"))
    assert "service" not in schema["properties"]["sandbox"]["properties"]
