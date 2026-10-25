"""T-08 验收（端到端）：task-service 配了 Registry（docs/plan/registry.md 第 5 节“端到端”）。

用 registry-seed 种下模板与 Model Profile，task-service 以 AR_REGISTRY_ADDR 启用 Registry（不再读固定版本文件）：
- 新 Task 的 Task State 里固定的是提交那一刻的默认版本；Learning Pipeline 经 Registry 发布并晋升新版本之后，
  只有新 Task 用新版本，先提交的 Task 不受影响（设计书 3.1.2“固定”、附录 J.5）。
- Python 的 GrpcRegistry 经 gRPC 读写 Go 的 RegistryService（跨语言：租户、分页、发布令牌、错误还原）。
- RegistryService 只在单独的内部端口上，Task API 的端口上没有它。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from agentruntime.registry.v1 import registry_pb2 as rg
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.errors import DeniedError

from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.registry import GrpcRegistry

from e2e_support.api import TaskApi, finished_payload, stream_until_finished
from e2e_support.stack import REPO_ROOT, Stack, free_port

pytestmark = pytest.mark.e2e

TOKEN = "e2e-registry-publish-token-41ab"
PROFILE_ID = "mock/model@2026-10"
ANSWER_V1 = {"description": "直接作答", "executor": "model", "effect": "none", "role": "deliver", "output": "generate"}
ANSWER_V2 = {**ANSWER_V1, "description": "直接作答，先列要点"}
SEED = {
    "entries": [
        {"kind": "node_template", "name": "answer", "document": ANSWER_V1},
        {"kind": "node_template", "name": "sql_query", "document": {"description": "查询分析库", "executor": "program", "effect": "read", "role": "fetch"}},
        {
            "kind": "model_profile",
            "name": PROFILE_ID,
            "document": {
                "id": PROFILE_ID,
                "vendor": "mock",
                "model": "model",
                "class": "mid",
                "tiers": [{"name": "low"}],
                "capabilities": ["generate"],
                "cost": {"in_per_m": 0, "out_per_m": 0, "cache_read_per_m": 0},
                "freshness": {"price_at": "2026-10-22"},
            },
        },
    ]
}
EVIDENCE = {
    "eval_set": {"name": "task-loop", "version": "c0117be"},
    "samples": 26,
    "candidate": {"success_rate": 0.92, "cost_per_success_usd": 0.05},
    "passed": True,
    "report": "e2e.json",
    "evaluated_at": "2026-10-22T09:00:00Z",
}


def seed(stack: Stack, entries: dict[str, Any]) -> None:
    path = stack.root / "registry-seed.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), "utf-8")
    assert stack.task_service_bin is not None
    proc = subprocess.run(
        [str(stack.task_service_bin), "registry-seed", str(path)],
        env=stack.cli_env({"AR_SCHEMAS_DIR": str(REPO_ROOT / "schemas")}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.returncode == 0, f"registry-seed 失败：\n{proc.stderr}"


@pytest.fixture
def registry(stack: Stack) -> Iterator[str]:
    """种下 Registry、写入发布令牌，以 Registry 重启 task-service；结束后恢复默认（固定版本文件）。返回 Registry 的地址。"""
    seed(stack, SEED)
    LocalFileSecretStoreProvider(stack.secrets_file).seed_secret(None, "registry/publish-token", TOKEN)
    address = f"127.0.0.1:{free_port()}"
    stack.restart_task_service({"AR_REGISTRY_ADDR": address, "AR_PINNED_VERSIONS_FILE": "", "AR_SCHEMAS_DIR": str(REPO_ROOT / "schemas")})
    try:
        yield address
    finally:
        stack.restart_task_service(None)


def pinned(api: TaskApi, task_id: str) -> dict[str, Any]:
    return api.query(task_id)["taskState"]["value"]["pinned"]


async def default_version(address: str, name: str) -> str:
    """当前的默认版本。端到端环境在整个会话里共用：别的用例可能已经发布过，版本号不一定从 v1 起。"""
    client = GrpcRegistry(address)
    try:
        return (await client.get_node_template(Tenant(tenant_id="t1", user_id="u1"), name)).version
    finally:
        await client.aclose()


async def release_answer_v2(address: str, current: str) -> str:
    client = GrpcRegistry(address, publish_token=TOKEN)
    try:
        published = await client.publish(None, rg.REGISTRY_KIND_NODE_TEMPLATE, "answer", ANSWER_V2, change_note="端到端测试")
        assert published.state == rg.RELEASE_STATE_SHADOW
        promoted = await client.promote(None, rg.REGISTRY_KIND_NODE_TEMPLATE, "answer", published.version, EVIDENCE)
        assert promoted.previous_default_version == current
        return published.version
    finally:
        await client.aclose()


def test_tasks_pin_the_registry_defaults_and_only_new_tasks_see_a_promotion(stack: Stack, api: TaskApi, registry: str) -> None:
    answer = asyncio.run(default_version(registry, "answer"))
    first = api.submit()
    assert finished_payload(stream_until_finished(first["eventsUrl"], api.token or ""))["status"] == "TASK_STATUS_DONE"
    # sql_query 与 Model Profile 的内容从不变，重复种下也跳过，所以一直是 v1。
    expected = {"recipe": None, "node_templates": {"answer": answer, "sql_query": "v1"}, "model_profiles": {PROFILE_ID: "v1"}}
    assert pinned(api, first["taskId"]) == expected, "Task State 里固定的是 Registry 在提交那一刻的默认版本"

    released = asyncio.run(release_answer_v2(registry, answer))
    assert released != answer

    second = api.submit()
    assert finished_payload(stream_until_finished(second["eventsUrl"], api.token or ""))["status"] == "TASK_STATUS_DONE"
    assert pinned(api, second["taskId"])["node_templates"] == {"answer": released, "sql_query": "v1"}, "晋升之后的新 Task 用新版本"
    assert pinned(api, first["taskId"]) == expected, "先提交的 Task 不受影响"


def test_the_python_client_reads_and_writes_the_go_registry(registry: str) -> None:
    async def scenario() -> None:
        reader = GrpcRegistry(registry)
        writer = GrpcRegistry(registry, publish_token="not-the-token")
        try:
            templates = await reader.list_node_templates(Tenant(tenant_id="t1", user_id="u1"), None)
            assert {t.name for t in templates} >= {"answer", "sql_query"}
            assert all(t.state == rg.RELEASE_STATE_DEFAULT for t in templates), "不带固定版本时列的是默认版本"
            with pytest.raises(DeniedError) as caught:
                await writer.publish(None, rg.REGISTRY_KIND_NODE_TEMPLATE, "answer", ANSWER_V2)
            assert "not-the-token" not in str(caught.value) and TOKEN not in str(caught.value)
        finally:
            await reader.aclose()
            await writer.aclose()

    asyncio.run(scenario())


def test_the_registry_is_not_served_on_the_task_api_port(stack: Stack, registry: str) -> None:
    request = urllib.request.Request(
        stack.http_url + "/agentruntime.registry.v1.RegistryService/ListNodeTemplates",
        data=json.dumps({"tenant": {"tenantId": "t1"}}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    assert caught.value.code == 404, "Task API 的端口上没有 RegistryService"
