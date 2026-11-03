"""docs/plan/running-task-interaction.md 第 3 节验收：一次模型调用的上下文里各类内容占多少（参考 Claude Code 的 /context）。

- ContextBuilder.build 在组装时算出各类的 token 估算，不新增模型调用：System prompt、System tools、MCP tools、Memory、
  Task brief、Messages；Free space 由客户端用窗口减各类之和。
- 分母是模型的真实窗口（Model Gateway 从 Model Profile 带回来），另带 Runtime 的压缩线（effective_tokens）；取不到窗口时用压缩线。
- 每次模型调用之后推一条 context_usage：只有数字与类别名，没有内容；还带用过的压缩步骤。fast 的 answer 节点也算一份。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from agentruntime.common.v1 import payload_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_worker import names
from agentruntime_worker.activities.types import NodeInput, ToolLoopStep
from agentruntime_worker.context.builder import ContextBuilder
from agentruntime_worker.context.tokens import estimate
from agentruntime_worker.executor.tool_loop import ModelToolLoop

from .context_support import SYSTEM, ContextGateway, config, delta, history, objects, read_step, request
from .executor_harness import TASK_ID, make_fixture
from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .support import assert_valid
from .test_node_executor import env_for, tenant_proto
from .test_parallel_tool_calls import FINISH, read, respond, script_loop
from .test_progress_events import progress
from .test_stage_runner import deliver, goal, one_round, scripted_steps
from .test_tool_loop_approval import IRREVERSIBLE
from .test_tool_loop_model import step_input

CATEGORIES = {"system_prompt", "system_tools", "mcp_tools", "memory", "task_brief", "messages"}
USAGE = {
    "window_tokens": 200_000,
    "effective_tokens": 32_000,
    "total_tokens": 700,
    "categories": [{"name": "system_prompt", "tokens": 100}, {"name": "messages", "tokens": 600}],
}


def spec(name: str, description: str = "工具说明") -> mg.ToolSpec:
    return mg.ToolSpec(name=name, description=description, params_schema=payload_pb2.JsonPayload())


def by_name(usage: Any) -> dict[str, int]:
    return {c["name"]: c["tokens"] for c in usage.categories}


async def built(tmp_path: Path, **extra: Any) -> Any:
    steps = extra.pop("steps", [read_step(0)])
    gateway = ContextGateway(summaries=extra.pop("summaries", None))
    cfg = extra.pop("cfg", config(effective_tokens=100_000))
    system = extra.pop("system", None)
    req = request(steps, **extra)
    return await ContextBuilder(gateway, objects(tmp_path), cfg).build(dataclasses.replace(req, system=system) if system else req)


# --- Context Builder ---


async def test_every_category_is_there_and_they_add_up(tmp_path: Path) -> None:
    b = await built(tmp_path, tools=[spec("repo_read"), spec("mcp.search")], mcp_tools=frozenset({"mcp.search"}), profile=["汇报用表格"], conversation="用户：之前的话")
    counts = by_name(b.usage)
    assert set(counts) == CATEGORIES
    assert all(v > 0 for v in counts.values()), counts
    assert sum(counts.values()) == b.usage.total_tokens == b.tokens, "和 Context Builder 算阈值用的是同一个数"


async def test_empty_categories_are_zero_not_missing(tmp_path: Path) -> None:
    counts = by_name((await built(tmp_path)).usage)
    assert set(counts) == CATEGORIES
    assert counts["system_tools"] == 0 and counts["mcp_tools"] == 0 and counts["memory"] == 0


async def test_the_system_prompt_counts_only_the_system_partition(tmp_path: Path) -> None:
    long = await built(tmp_path, system=[SYSTEM + "补充说明" * 200])
    short = await built(tmp_path / "x", system=[SYSTEM])
    assert by_name(long.usage)["system_prompt"] > by_name(short.usage)["system_prompt"]
    assert by_name(long.usage)["messages"] == by_name(short.usage)["messages"]
    assert by_name(long.usage)["task_brief"] == by_name(short.usage)["task_brief"]


async def test_tools_split_into_system_and_mcp(tmp_path: Path) -> None:
    alone = await built(tmp_path, tools=[spec("repo_read")])
    mixed = await built(tmp_path / "x", tools=[spec("repo_read"), spec("mcp.search", "很长的说明" * 50)], mcp_tools=frozenset({"mcp.search"}))
    assert by_name(mixed.usage)["system_tools"] == by_name(alone.usage)["system_tools"]
    assert by_name(alone.usage)["mcp_tools"] == 0 and by_name(mixed.usage)["mcp_tools"] > 0


async def test_each_tool_is_listed_with_its_own_tokens(tmp_path: Path) -> None:
    """工具默认只看合计，展开能看到每个工具占多少。"""
    usage = (await built(tmp_path, tools=[spec("repo_read"), spec("mcp.search", "很长的说明" * 50)], mcp_tools=frozenset({"mcp.search"}))).usage
    listed = {t["name"]: t for t in usage.tools}
    assert set(listed) == {"repo_read", "mcp.search"}
    assert listed["mcp.search"]["mcp"] is True and not listed["repo_read"].get("mcp")
    assert listed["mcp.search"]["tokens"] > listed["repo_read"]["tokens"]
    counts = by_name(usage)
    assert counts["system_tools"] == listed["repo_read"]["tokens"]
    assert counts["mcp_tools"] == listed["mcp.search"]["tokens"]


async def test_memory_is_the_profile_partition(tmp_path: Path) -> None:
    b = await built(tmp_path, profile=["用户偏好" * 100])
    assert by_name(b.usage)["memory"] >= estimate("用户偏好" * 100)


async def test_earlier_conversation_is_messages_not_task_brief(tmp_path: Path) -> None:
    """同一 Session 之前的对话放在 brief 分区（不破坏提示缓存），但对用户来说它是 Messages。"""
    text = "用户：之前的话。" * 200
    without = await built(tmp_path)
    withit = await built(tmp_path / "x", conversation=text)
    assert by_name(withit.usage)["task_brief"] == by_name(without.usage)["task_brief"]
    assert by_name(withit.usage)["messages"] >= by_name(without.usage)["messages"] + estimate(text) * 0.9


async def test_history_is_messages_and_grows_with_it(tmp_path: Path) -> None:
    short = await built(tmp_path, steps=history(1))
    long = await built(tmp_path / "x", steps=history(6))
    assert by_name(long.usage)["messages"] > by_name(short.usage)["messages"]
    assert by_name(long.usage)["task_brief"] == by_name(short.usage)["task_brief"]


async def test_compaction_shrinks_messages_and_is_reported(tmp_path: Path) -> None:
    steps = history(40, chars=2_000)
    uncompacted = await built(tmp_path / "a", steps=steps, cfg=config(effective_tokens=1_000_000))
    compacted = await built(tmp_path / "b", steps=steps, cfg=config(effective_tokens=4_000), summaries=[delta(intent=["修 CI"])])
    assert compacted.usage.applied == compacted.applied and compacted.applied
    assert by_name(compacted.usage)["messages"] < by_name(uncompacted.usage)["messages"]
    assert compacted.usage.total_tokens == compacted.tokens


async def test_usage_has_only_numbers_and_names(tmp_path: Path) -> None:
    secret = "上下文正文不该出现"
    b = await built(tmp_path, steps=[{"tool": "repo_read", "args": {"path": secret}, "observation": {"text": secret}}], profile=[secret], conversation=secret, system=[SYSTEM + secret])
    assert secret in json.dumps([m.text for m in b.messages], ensure_ascii=False), "上下文里确实有它"
    assert secret not in json.dumps({"categories": b.usage.categories, "tools": b.usage.tools, "applied": b.usage.applied}, ensure_ascii=False)


async def test_building_usage_makes_no_extra_model_calls(tmp_path: Path) -> None:
    gateway = ContextGateway()
    await ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=100_000)).build(request([read_step(0)]))
    assert gateway.requests == []


# --- tool_loop 的一步 ---


def answered(window: int) -> Any:
    def reply(request: mg.GenerateRequest) -> mg.GenerateResponse:
        return mg.GenerateResponse(finish_reason=mg.FINISH_REASON_STOP, text="完成", model=mg.ModelChoice(model_profile_id="m", tier="strong@medium", vendor="v", context_window=window))

    return reply


def step_with(tmp_path: Path, window: int) -> ModelToolLoop:
    gateway = ContextGateway(steps=[answered(window)])
    return ModelToolLoop(gateway, ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=32_000)))


async def test_a_step_reports_usage_with_the_models_real_window(tmp_path: Path) -> None:
    step = await step_with(tmp_path, 200_000).step(step_input([]))
    usage = step.context_usage
    assert usage["window_tokens"] == 200_000 and usage["effective_tokens"] == 32_000
    assert usage["total_tokens"] == sum(c["tokens"] for c in usage["categories"])
    assert {c["name"] for c in usage["categories"]} == CATEGORIES
    assert_valid("task_event_payload", {"event": "context_usage", "round_seq": 1, "node_id": "fix", "step": 1, **usage})


async def test_without_a_window_the_compaction_line_is_used(tmp_path: Path) -> None:
    """网关没配 context_window 时是 0。schema 要求窗口至少是 1。"""
    usage = (await step_with(tmp_path, 0).step(step_input([]))).context_usage
    assert usage["window_tokens"] == usage["effective_tokens"] == 32_000


async def test_a_step_counts_registered_mcp_tools_separately(tmp_path: Path) -> None:
    """哪些是 MCP 工具由 Worker 启动时的 MCP 会话给出（tools.mcp_tool_names）。"""
    gateway = ContextGateway(steps=[answered(200_000)])
    loop = ModelToolLoop(gateway, ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=32_000)), mcp_tools=lambda: frozenset({"run_tests"}))
    usage = (await loop.step(step_input([]))).context_usage
    listed = {t["name"]: t for t in usage["tools"]}
    assert listed["run_tests"]["mcp"] is True and "mcp" not in listed["repo_read"]
    assert {c["name"]: c["tokens"] for c in usage["categories"]}["mcp_tools"] == listed["run_tests"]["tokens"]


async def test_the_answer_node_reports_usage_too(tmp_path: Path) -> None:
    """fast 的 answer 节点不经 Context Builder，也要算一份：系统提示与对话。"""
    fx = make_fixture(tmp_path)
    fx.models.reply({"answer": "你好"})
    node = {"id": "answer", "kind": "atomic", "template": "answer@v1", "executor": "model", "output": "generate", "effect": "none", "role": "deliver", "inputs": {"request": "你好吗"}}
    out = await env_for(names.EXECUTE_MODEL_NODE, queue=names.MODEL_QUEUE).run(fx.executor.execute_model_node, NodeInput(tenant=tenant_proto(), task_id=TASK_ID, round_seq=1, node=node))
    usage = out["context_usage"]
    assert {c["name"] for c in usage["categories"]} == {"system_prompt", "messages"}
    assert usage["total_tokens"] > 0
    assert_valid("task_event_payload", {"event": "context_usage", "round_seq": 1, "node_id": "answer", **usage})


async def test_other_model_nodes_do_not_report_usage(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path)
    fx.models.reply({"ok": True})
    node = {"id": "judge", "kind": "atomic", "template": "summarize@v1", "executor": "model", "output": "generate", "effect": "none", "role": "transform", "inputs": {"text": "x"}}
    out = await env_for(names.EXECUTE_MODEL_NODE, queue=names.MODEL_QUEUE).run(fx.executor.execute_model_node, NodeInput(tenant=tenant_proto(), task_id=TASK_ID, round_seq=1, node=node))
    assert "context_usage" not in out


# --- 工作流推事件 ---

def usage_events(h: StageHarness, task_id: str) -> list[dict[str, Any]]:
    return [e for e in progress(h, task_id) if e["event"] == "context_usage"]


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_every_model_step_publishes_context_usage(harness: StageHarness) -> None:
    script_loop(
        harness,
        ToolLoopStep(action="tool", tool="repo_read", args={"path": "a.py"}, calls=[read("a.py")], context_usage=USAGE),
        ToolLoopStep(action="finish", result={"done": True}, context_usage={**USAGE, "total_tokens": 900, "categories": [{"name": "messages", "tokens": 900}], "applied": ["mask"]}),
    )
    inp, result = await harness.run(effect_ceiling=IRREVERSIBLE)
    events = usage_events(harness, inp.task_id)
    assert [(e["node_id"], e["step"], e["round_seq"]) for e in events] == [("fix", 1, 1), ("fix", 2, 1)]
    assert events[0]["window_tokens"] == 200_000
    assert events[1]["total_tokens"] == 900 and events[1]["applied"] == ["mask"]
    kinds = [e["event"] for e in progress(harness, inp.task_id)]
    assert kinds.index("context_usage") < kinds.index("tool_status"), "模型调用之后、工具调用之前"


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_step_without_usage_publishes_nothing(harness: StageHarness) -> None:
    """桩的 tool_loop 没有 Context Builder。"""
    script_loop(harness, respond(read("a.py")), FINISH)
    inp, _ = await harness.run(effect_ceiling=IRREVERSIBLE)
    assert usage_events(harness, inp.task_id) == []


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_top_level_sub_agent_publishes_its_own_context_usage(harness: StageHarness) -> None:
    """N-16（N16_PATCH）之后顶层的 sub_agent 在子工作流里推自己的 context_usage（同 tool_status），node_id 是它：客户端画成并行的子 Agent。"""
    s = harness.script
    s.tools["web_search"] = "read"
    s.tool_steps["research"] = scripted_steps(ToolLoopStep(action="finish", result={"found": True}, context_usage=USAGE))
    one_round(harness, goal("research", "sub_agent", tools=["web_search"]), deliver(after=["research"]))
    inp, _ = await harness.run()
    assert [e["node_id"] for e in usage_events(harness, inp.task_id)] == ["research"]


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_the_answer_node_publishes_context_usage_without_a_step(harness: StageHarness) -> None:
    s = harness.script

    async def answering(inp: NodeInput, script: Any) -> dict[str, Any]:
        return {"result": {"answer": "你好"}, "context_usage": USAGE}

    s.behaviors["answer"] = answering
    one_round(harness, deliver())
    inp, _ = await harness.run()
    [event] = usage_events(harness, inp.task_id)
    assert event["node_id"] == "answer" and "step" not in event


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_failed_publish_does_not_fail_the_task(harness: StageHarness) -> None:
    from temporalio.exceptions import ApplicationError

    from agentruntime.common.v1 import enums_pb2

    s = harness.script

    def refuse(key: str, event: Any) -> None:
        if "context_usage" in json.dumps(dict(event.payload.value.struct_value.fields.items()), default=str):
            raise ApplicationError("推不出去", non_retryable=True)

    s.on_publish = refuse
    script_loop(harness, ToolLoopStep(action="finish", result={"done": True}, context_usage=USAGE))
    _, result = await harness.run(effect_ceiling=IRREVERSIBLE)
    assert result.status == enums_pb2.TASK_STATUS_DONE

