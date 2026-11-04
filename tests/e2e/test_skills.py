"""N-14 验收（端到端）：task-service 配了 Registry 时的用户 Skill（docs/plan/n14-skill-contract.md）。

- SkillService 在 Task API 的端口上，经 Ingress 认证：本人发布、列出、读、收回自己的；同租户的别人读不到；
  请求里的 tenant_id 与认证身份不一致时 PermissionDenied。
- 新 Task 固定提交者看得到的 Skill（Registry 的 ResolvePinnedVersions，跨 Go 与 Python）：发布 v2 之后新 Task 固定 v2，
  按 v1 照旧读得到 v1；有 Skill 时 Task 照常跑完（提交时把固定的 Skill 记在 Task 的记录上）。
- Python 的 GrpcRegistry 读 Go 的 ListSkills、GetSkill：目录不带正文，用户层按 TenantContext.user_id。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from agentruntime.common.v1 import tenant_pb2
from agentruntime.registry.v1 import registry_pb2 as rg
from agentruntime.registry.v1 import registry_pb2_grpc as rg_grpc
from google.protobuf import json_format

from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.registry import GrpcRegistry

from e2e_support.api import ApiError, TaskApi, finished_payload, stream_until_finished
from e2e_support.stack import Stack
from test_registry import registry  # noqa: F401 — fixture：以 Registry 重启 task-service

pytestmark = pytest.mark.e2e

SERVICE = "/agentruntime.skill.v1.SkillService/"


def skill_call(stack: Stack, token: str, method: str, body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        stack.http_url + SERVICE + method, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        payload = json.loads(err.read() or b"{}")
        reason = next((d.get("debug", {}).get("reason", "") for d in payload.get("details") or [] if d.get("type") == "google.rpc.ErrorInfo"), "")
        raise ApiError(err.code, reason or payload.get("code", ""), payload.get("message", "")) from None


def put(stack: Stack, token: str, name: str, instructions: str) -> dict[str, Any]:
    doc = {"description": "写周报：按模块归类", "instructions": instructions}
    return skill_call(stack, token, "PutSkill", {"tenantId": "t1", "name": name, "document": {"value": doc}})


async def resolve(address: str, user: str) -> dict[str, str]:
    import grpc

    channel = grpc.aio.insecure_channel(address)
    try:
        stub = rg_grpc.RegistryServiceStub(channel)
        resp = await stub.ResolvePinnedVersions(rg.ResolvePinnedVersionsRequest(tenant=tenant_pb2.TenantContext(tenant_id="t1", user_id=user)), timeout=10)
        return dict(resp.pinned_versions.skills)
    finally:
        await channel.close()


def test_user_skills_through_the_task_api_port_and_the_registry(stack: Stack, api: TaskApi, registry: str) -> None:  # noqa: F811
    alice, bob = stack.token("t1", "e2e-alice"), stack.token("t1", "e2e-bob")
    first = put(stack, alice, "e2e-weekly", "第一版")
    assert first["created"] is True and first["skill"]["version"] == "user/v1" and first["skill"]["owner"] == "e2e-alice"
    again = put(stack, alice, "e2e-weekly", "第一版")
    assert again["skill"]["version"] == "user/v1" and not again.get("newVersion"), "内容不变不出新版本"

    listed = skill_call(stack, alice, "ListSkills", {"tenantId": "t1"})["skills"]
    mine = [s for s in listed if s["name"] == "e2e-weekly"]
    assert mine and "document" not in mine[0], "目录不带正文"
    with pytest.raises(ApiError) as hidden:
        skill_call(stack, bob, "GetSkill", {"tenantId": "t1", "name": "e2e-weekly"})
    assert hidden.value.status == 404 and hidden.value.code == "skill_not_found", "同租户的别人读不到"
    with pytest.raises(ApiError) as mismatch:
        skill_call(stack, alice, "ListSkills", {"tenantId": "t2"})
    assert mismatch.value.status == 403

    pins = asyncio.run(resolve(registry, "e2e-alice"))
    assert pins.get("e2e-weekly") == "user/v1"
    assert "e2e-weekly" not in asyncio.run(resolve(registry, "e2e-bob")), "别人的 Task 不固定它"

    second = put(stack, alice, "e2e-weekly", "第二版")
    assert second["skill"]["version"] == "user/v2"
    assert asyncio.run(resolve(registry, "e2e-alice"))["e2e-weekly"] == "user/v2", "新 Task 固定 v2"
    old = skill_call(stack, alice, "GetSkill", {"tenantId": "t1", "name": "e2e-weekly", "version": "user/v1"})["skill"]
    assert old["document"]["value"]["instructions"] == "第一版", "按 v1 照旧读得到 v1"

    async def python_client() -> None:
        client = GrpcRegistry(registry)
        try:
            tenant = Tenant(tenant_id="t1", user_id="e2e-alice")
            got = await client.list_skills(tenant, {"e2e-weekly": "user/v1"})
            assert [s.name for s in got] == ["e2e-weekly"] and not got[0].HasField("document")
            body = await client.get_skill(tenant, "e2e-weekly", "user/v2")
            assert json_format.MessageToDict(body.document.value)["instructions"] == "第二版"
        finally:
            await client.aclose()

    asyncio.run(python_client())

    # 提交者有 Skill 时 Task 照常跑完（提交时把固定的 Skill 记在 Task 的记录上）。
    task = TaskApi(stack.http_url, alice).submit()
    assert finished_payload(stream_until_finished(task["eventsUrl"], alice))["status"] == "TASK_STATUS_DONE"

    retired = skill_call(stack, alice, "RetireSkill", {"tenantId": "t1", "name": "e2e-weekly"})
    assert retired["retired"] is True and retired["version"] == "user/v2"
    assert "e2e-weekly" not in asyncio.run(resolve(registry, "e2e-alice")), "收回后新 Task 不再固定它"
