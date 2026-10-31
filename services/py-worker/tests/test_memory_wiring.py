"""docs/plan/memory-live.md M-03 验收：Worker 接 Memory Service。没配时与现在一样；配了才写和读；检索失败不拖垮 Task。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import payload_pb2
from agentruntime.memory.v1 import memory_pb2
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.memory import RetrievalContextSource, WorkerMemoryClient, memory_client, node_experiences
from agentruntime_worker.planner.context import CatalogView, PlanningContext, StaticContextSource
from agentruntime_worker.reviewer.wrap_up import RecordingMemoryClient

from .executor_harness import TASK_ID, TENANT
from .test_planner_activity import plan_input

SECRET_TEXT = "用户的私密任务描述"


def payload(title: str) -> payload_pb2.JsonPayload:
    value = struct_pb2.Value()
    json_format.ParseDict({"content": {"title": title, "guidance": "先跑失败的测试"}, "provenance": {"source": "agent_reflection"}, "validity": {"valid_from": "2026-10-01"}, "trust": 0.6}, value)
    return payload_pb2.JsonPayload(value=value)


class FakeMemory:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.requests: list[memory_pb2.SearchRequest] = []

    async def search(self, request: memory_pb2.SearchRequest) -> memory_pb2.SearchResponse:
        self.requests.append(request)
        if self.fail:
            raise self.fail
        return memory_pb2.SearchResponse(items=[memory_pb2.RetrievedItem(memory_item=payload("修测试的经验"), score=0.9)])


def base_context() -> PlanningContext:
    return PlanningContext(risk_authorized="read", effect_ceiling="read", catalog=CatalogView())


def config(tmp_path: Path, **env: str) -> WorkerConfig:
    return WorkerConfig.from_env({"AR_DATA_DIR": str(tmp_path), **env})


def test_without_an_address_nothing_changes(tmp_path: Path) -> None:
    assert isinstance(memory_client(config(tmp_path)), RecordingMemoryClient)


def test_with_an_address_the_real_client_is_used(tmp_path: Path) -> None:
    client = memory_client(config(tmp_path, AR_MEMORY_ADDRESS="memory:50061", AR_DEV_SIGNING_KEY="k" * 32))
    assert isinstance(client, WorkerMemoryClient)


def test_an_address_without_a_signing_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        memory_client(config(tmp_path, AR_MEMORY_ADDRESS="memory:50061"))


async def test_planning_searches_for_this_task_and_gets_experiences() -> None:
    memory = FakeMemory()
    source = RetrievalContextSource(StaticContextSource(base_context()), memory)
    inp = plan_input(SECRET_TEXT)
    context = await source.load(inp)
    [request] = memory.requests
    assert request.trigger == memory_pb2.SEARCH_TRIGGER_PLANNING
    assert request.tenant.tenant_id == inp.tenant.tenant_id and request.query == SECRET_TEXT
    assert request.token_budget > 0 and request.top_k > 0
    assert len(context.experiences) == 1 and "修测试的经验" in context.experiences[0]


@pytest.mark.parametrize("failure", [TimeoutError("slow"), RuntimeError(SECRET_TEXT)])
async def test_a_failed_search_does_not_stop_planning(failure: Exception, caplog: pytest.LogCaptureFixture) -> None:
    source = RetrievalContextSource(StaticContextSource(base_context()), FakeMemory(fail=failure))
    with caplog.at_level(logging.DEBUG):
        context = await source.load(plan_input(SECRET_TEXT))
    assert context.experiences == ()
    assert SECRET_TEXT not in caplog.text, "任务内容不进日志"


async def test_a_tool_loop_searches_on_the_first_step_only() -> None:
    memory = FakeMemory()
    node: dict[str, Any] = {"id": "fix", "goal": "修测试", "tools": ["repo_edit"]}
    first = await node_experiences(memory, TENANT, TASK_ID, node, iteration=1)
    later = await node_experiences(memory, TENANT, TASK_ID, node, iteration=2)
    assert len(first) == 1 and later == first, "之后的步骤接着用第一步查到的，不再查"
    [request] = memory.requests
    assert request.trigger == memory_pb2.SEARCH_TRIGGER_NODE_START and list(request.tools) == ["repo_edit"]


async def test_a_failed_node_search_gives_no_experiences() -> None:
    assert await node_experiences(FakeMemory(fail=RuntimeError("x")), TENANT, TASK_ID, {"id": "n", "goal": "g", "tools": []}, iteration=1) == ()


def test_the_token_names_the_task_tenant() -> None:
    client = WorkerMemoryClient("memory:50061", b"k" * 32)
    metadata = dict(client.metadata(TENANT.tenant_id, TENANT.user_id))
    assert metadata["authorization"].startswith("Bearer ")
    assert client.metadata("t-other", "u")[0][1] != metadata["authorization"], "令牌按请求的租户签"
