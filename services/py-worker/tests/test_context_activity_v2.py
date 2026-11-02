"""Q87–Q93：真实 ContextBuilder / Activity，及旧输入的兼容路径。"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.errors import InvalidError

from agentruntime_worker import names
from agentruntime_worker.activities.types import NodeInput, ToolLoopStepInput
from agentruntime_worker.context.builder import ContextBuilder, ContextConfig
from agentruntime_worker.context.partitions import STATE_HEADER
from agentruntime_worker.executor.tool_loop import ModelToolLoop

from .context_support import ContextGateway, TASK_PLAN, delta, history, objects, request, text_of
from .executor_harness import make_fixture, model, node_input
from .test_node_executor import env_for


async def test_v2_preserves_complete_state_and_system_beyond_old_state_limit(tmp_path: Path) -> None:
    state = {"facts": [f"fact-{i}: exact evidence" for i in range(300)], "pinned": {"recipe": "v9"}, "rounds": [{"seq": 1}]}
    system = ["must preserve all system constraints"]
    builder = ContextBuilder(ContextGateway(), objects(tmp_path), ContextConfig(effective_tokens=30_000, state_tokens=10))
    req = dataclasses.replace(request(history(3)), task_state=state, system=system, context_version=2)
    built = await builder.build(req)
    precise = next(m.text for m in built.messages if m.text.startswith(STATE_HEADER))
    assert json.loads(precise.split("\n", 1)[1]) == state
    assert built.messages[0].text == system[0]
    assert req.task_state == state


async def test_v2_compacts_older_steps_but_preserves_recent_results(tmp_path: Path) -> None:
    steps = history(7, chars=600)
    gateway = ContextGateway(summaries=[delta(decisions=["older results summarized"])])
    builder = ContextBuilder(gateway, objects(tmp_path), ContextConfig(effective_tokens=3_500, offload_tokens=80, probe=False))
    built = await builder.build(dataclasses.replace(request(steps), context_version=2))
    text = text_of(built.messages)
    assert "offload" in built.applied
    assert steps[0]["observation"]["content"] not in text
    for step in steps[-2:]:
        assert next(v for k, v in step["observation"].items() if k in ("content", "stdout")) in text


async def test_v2_reset_does_not_summarize_recent_messages(tmp_path: Path) -> None:
    steps = [{"role": "user", "content": f"message-{i}: " + "older material " * 100} for i in range(7)]
    gateway = ContextGateway(summaries=[delta(decisions=["old messages summarized"])])
    builder = ContextBuilder(gateway, objects(tmp_path), ContextConfig(effective_tokens=3_000, probe=False))
    built = await builder.build(dataclasses.replace(request(steps), context_version=2))
    assert "summarize" in built.applied
    assert steps[0]["content"] not in text_of(built.messages)
    assert all(step["content"] in text_of(built.messages) for step in steps[-2:])


async def test_v2_reports_irreducible_overflow_without_truncating_state(tmp_path: Path) -> None:
    builder = ContextBuilder(ContextGateway(), objects(tmp_path), ContextConfig(effective_tokens=300, probe=False))
    req = dataclasses.replace(request([]), task_state={"facts": ["must stay exact " * 400]}, context_version=2)
    with pytest.raises(InvalidError, match="context_window_exceeded"):
        await builder.build(req)
    assert len(req.task_state["facts"][0]) == len("must stay exact " * 400)


async def test_real_model_activity_roundtrips_snapshot_and_full_state(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path)
    fx.models.reply({"summary": "done"})
    builder = ContextBuilder(fx.models, fx.objects, ContextConfig(effective_tokens=30_000, state_tokens=10, probe=False))
    fx.executor.deps = dataclasses.replace(fx.executor.deps, context_builder=builder)
    inp = node_input(model("ctx"))
    inp.context_version = 2
    inp.task_state = {"goal": "check", "facts": [f"keep-{i}" for i in range(300)], "pinned": {"recipe": "v3"}}
    inp.task_plan = TASK_PLAN
    inp.history = [{"node_id": "previous", "observation": {"validated": True}}]
    inp.injections = [{"zone": "constraints", "content": "retain exact state"}]
    converter = DataConverter.default
    restored, = await converter.decode(await converter.encode([inp]), [NodeInput])
    out = await env_for(names.EXECUTE_MODEL_NODE, queue=names.MODEL_QUEUE).run(fx.executor.execute_model_node, restored)
    assert out["result"] == {"summary": "done"}
    generated = fx.models.requests[-1]
    precise = next(m.text for m in generated.generation.messages if m.text.startswith(STATE_HEADER))
    assert json.loads(precise.split("\n", 1)[1]) == inp.task_state
    assert "previous" in text_of(generated.generation.messages)
    assert "retain exact state" in generated.generation.messages[0].text
    assert generated.call.task_family == TASK_PLAN["triage"]["task_family"]


@pytest.mark.parametrize("version,state,code", [
    (999, {"goal": "check"}, "unsupported_context_version"),
    (2, {"facts": ["keep exact " * 400]}, "context_window_exceeded"),
])
async def test_real_activity_rejects_invalid_context_before_model_call(tmp_path: Path, version: int, state: dict, code: str) -> None:
    fx = make_fixture(tmp_path)
    fx.executor.deps = dataclasses.replace(fx.executor.deps, context_builder=ContextBuilder(fx.models, fx.objects, ContextConfig(effective_tokens=300)))
    inp = node_input(model("ctx"))
    inp.task_state = state
    inp.context_version = version
    with pytest.raises(ApplicationError) as caught:
        await env_for(names.EXECUTE_MODEL_NODE, queue=names.MODEL_QUEUE).run(fx.executor.execute_model_node, inp)
    assert caught.value.type == "invalid"
    assert code in str(caught.value)
    assert not fx.models.requests


async def test_legacy_activity_input_keeps_legacy_prompt(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path)
    fx.models.reply({"summary": "legacy"})
    fx.executor.deps = dataclasses.replace(fx.executor.deps, context_builder=ContextBuilder(fx.models, fx.objects))
    raw = {"tenant": dataclasses.asdict(node_input(model("ctx")).tenant), "task_id": "old", "round_seq": 1, "node": model("ctx")}
    converter = DataConverter.default
    inp, = await converter.decode(await converter.encode([raw]), [NodeInput])
    assert inp.context_version == 1
    await env_for(names.EXECUTE_MODEL_NODE, queue=names.MODEL_QUEUE).run(fx.executor.execute_model_node, inp)
    assert json.loads(fx.models.requests[-1].generation.messages[1].text) == {"template": "summarize@v2", "inputs": inp.node["inputs"]}


async def test_v2_context_does_not_use_the_old_template_input_cache(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path)
    fx.models.reply({"summary": "first state"}, {"summary": "second state"})
    fx.executor.deps = dataclasses.replace(fx.executor.deps, context_builder=ContextBuilder(fx.models, fx.objects))
    inp = node_input(model("ctx", cache={"ttl_s": 60}))
    inp.context_version = 2
    inp.task_state = {"facts": ["first state"]}
    first = await env_for(names.EXECUTE_MODEL_NODE, queue=names.MODEL_QUEUE).run(fx.executor.execute_model_node, inp)
    inp.task_state = {"facts": ["second state"]}
    second = await env_for(names.EXECUTE_MODEL_NODE, queue=names.MODEL_QUEUE).run(fx.executor.execute_model_node, inp)
    assert first["result"] != second["result"]
    assert len(fx.models.requests) == 2


async def test_conversational_v2_activity_also_uses_real_builder(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path)
    fx.models.reply({"summary": "reply"})
    fx.executor.deps = dataclasses.replace(fx.executor.deps, context_builder=ContextBuilder(fx.models, fx.objects))
    inp = node_input(model("answer", template="answer@v1", inputs={"request": "latest user message"}))
    inp.context_version = 2
    inp.task_state = {"facts": ["verified state"]}
    inp.task_family = "conversation.reply"
    await env_for(names.EXECUTE_MODEL_NODE, queue=names.MODEL_QUEUE).run(fx.executor.execute_model_node, inp)
    req = fx.models.requests[-1]
    assert req.call.task_family == "conversation.reply"
    assert any(m.text.startswith(STATE_HEADER) for m in req.generation.messages)
    assert any(m.text == "latest user message" and m.role == mg.CHAT_ROLE_USER for m in req.generation.messages)


async def test_tool_loop_routes_with_task_family_and_version(tmp_path: Path) -> None:
    gateway = ContextGateway(steps=[{"ok": True}])
    builder = ContextBuilder(gateway, objects(tmp_path), ContextConfig(effective_tokens=20_000))
    req = request([])
    inp = ToolLoopStepInput(tenant=req.tenant, task_id=req.task_id, round_seq=1, node=req.node, iteration=1,
                           history=[], task_state={"facts": ["exact"]}, task_plan=TASK_PLAN, context_version=2)
    await ModelToolLoop(gateway, builder).step(inp)
    assert gateway.requests[-1].call.task_family == TASK_PLAN["triage"]["task_family"]
    precise = next(m.text for m in gateway.requests[-1].generation.messages if m.text.startswith(STATE_HEADER))
    assert json.loads(precise.split("\n", 1)[1]) == inp.task_state
