"""R-03 评审 D11：外置内容按需取回（设计书 3.2.7 第 2 条、G.1“只以路径或 ID 出现，按需取回”）。

- 平台内工具 context_read（read）：按 obj:// 路径读本租户 context/ 与 node-results/ 下的对象，长内容按 offset 分段；
  别的租户的路径一律 not_found，别的前缀 denied。不要工作区配置，总是登记。
- Context Builder 外置的内容与 context_read 读的是同一个 ObjectStore（context_objects）。
- Planner 给每个 tool_loop 的 Goal Node 加上 context_read：要已登记、且 Goal 的 effect 上限允许 read；只在 activity 里加，
  不改 Stage Runner。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.enums import Effect
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.context.builder import ContextBuilder, ContextConfig, tenant_context
from agentruntime_worker.executor.tool_loop import dev_tool_loop
from agentruntime_worker.planner.activities import PlannerActivities
from agentruntime_worker.planner.context import StaticContextSource
from agentruntime_worker.planner.planner import Planner
from agentruntime_worker.tools import CONTEXT_READ, ToolCatalog, context_objects, dev_tools, latest

from .context_support import TASK_ID, TENANT, ContextGateway, read_step, request
from .planner_support import PLAN_STEPS, FakeGateway, context, signals
from .support import assert_valid
from .test_planner_activity import plan_input

T1 = TenantContext(tenant_id="t1", user_id="u1")
T2 = TenantContext(tenant_id="t2", user_id="u2")


def worker_config(tmp_path: Path) -> WorkerConfig:
    return WorkerConfig(data_dir=tmp_path, object_store_dir=tmp_path / "objects")


async def read(config: WorkerConfig, tenant: TenantContext, *, task_id: str = TASK_ID, **args: Any) -> dict[str, Any]:
    return (await dev_tools(config).bind(tenant).invoke(CONTEXT_READ, "v1", args, task_id=task_id)).output


# --- 工具 ---


async def test_context_read_returns_what_the_builder_offloaded(tmp_path: Path) -> None:
    config = worker_config(tmp_path)
    big = read_step(0, chars=20_000)
    builder = ContextBuilder(ContextGateway(), context_objects(config), ContextConfig(effective_tokens=100_000, offload_tokens=2_000))
    await builder.build(request([big, read_step(1)]))
    uri = context_objects(config).bind(tenant_context(TENANT)).uri(f"context/{TASK_ID}/r1/fix/step-0.json")

    whole = await read(config, T1, path=uri, max_chars=50_000)
    assert json.loads(whole["content"]) == big, "取回的就是外置前的完整输出"
    assert whole["truncated"] is False and whole["total_chars"] == len(whole["content"])
    part = await read(config, T1, path=uri, offset=100, max_chars=1_000)
    assert part["content"] == whole["content"][100:1_100] and part["truncated"] is True, "长内容按 offset 分段取"


async def test_context_read_reads_node_results_too(tmp_path: Path) -> None:
    config = worker_config(tmp_path)
    store = context_objects(config).bind(T1)
    await store.put(f"node-results/{TASK_ID}/q-abc.json", b'{"rows": [1, 2, 3]}', content_type="application/json")
    out = await read(config, T1, path=store.uri(f"node-results/{TASK_ID}/q-abc.json"))
    assert json.loads(out["content"]) == {"rows": [1, 2, 3]}


async def test_context_read_stays_inside_the_tenants_context_objects(tmp_path: Path) -> None:
    config = worker_config(tmp_path)
    store = context_objects(config).bind(T1)
    await store.put("context/x/r1/n/step-0.json", b"{}", content_type="application/json")
    await store.put("artifacts/report.json", b"{}", content_type="application/json")
    with pytest.raises(NotFoundError):
        await read(config, T2, path=store.uri("context/x/r1/n/step-0.json"))  # 别的租户的路径：当作不存在
    with pytest.raises(DeniedError):
        await read(config, T1, path=store.uri("artifacts/report.json"))  # 只读上下文与 Node 结果
    with pytest.raises(InvalidError):
        await read(config, T1, path="src/main.py")  # 不是 obj:// 路径
    with pytest.raises(NotFoundError):
        await read(config, T1, path=store.uri("context/x/r1/n/missing.json"))


async def test_context_read_is_always_registered_as_a_read_tool(tmp_path: Path) -> None:
    config = worker_config(tmp_path)
    specs = latest(await dev_tools(config).bind(T1).list_tools())
    assert specs[CONTEXT_READ].effect is Effect.READ
    assert "repo_read" not in specs, "工作区类工具仍要配置才登记"


@pytest.mark.parametrize("prefix", ["context", "node-results"])
async def test_context_read_denies_other_tasks_even_in_the_same_session(tmp_path: Path, prefix: str) -> None:
    config = worker_config(tmp_path)
    tenant = TenantContext(tenant_id="t1", user_id="u1", session_id=TENANT.session_id)
    store = context_objects(config).bind(tenant)
    key = f"{prefix}/{TASK_ID}-another/r1/result.json"
    await store.put(key, b'{"private": true}', content_type="application/json")
    with pytest.raises(NotFoundError):
        await read(config, tenant, path=store.uri(key), task_id=TASK_ID)
    with pytest.raises(DeniedError, match="no_task"):
        await read(config, tenant, path=store.uri(key), task_id="")


async def test_context_read_scope_is_enforced_through_real_node_activity(tmp_path: Path) -> None:
    from agentruntime_worker import names
    from .executor_harness import make_fixture, node_input, program
    from .test_node_executor import env_for
    from temporalio.exceptions import ApplicationError
    import dataclasses

    config = worker_config(tmp_path)
    fx = make_fixture(tmp_path / "executor")
    fx.executor.deps = dataclasses.replace(fx.executor.deps, tools=dev_tools(config))
    inp = node_input(program("read-context", CONTEXT_READ, inputs={"path": "unused"}))
    store = context_objects(config).bind(T1)
    for task in (inp.task_id, "another-task"):
        key = f"context/{task}/r1/n/v2/step-0.json"
        await store.put(key, b'{"ok": true}', content_type="application/json")
        inp.node["inputs"]["path"] = store.uri(key)
        if task == inp.task_id:
            result = await env_for(names.EXECUTE_PROGRAM_NODE).run(fx.executor.execute_program_node, inp)
            assert json.loads(result["result"]["content"]) == {"ok": True}
        else:
            with pytest.raises(ApplicationError, match="not_found"):
                await env_for(names.EXECUTE_PROGRAM_NODE).run(fx.executor.execute_program_node, inp)


def test_the_model_tool_loop_offloads_into_the_store_context_read_reads(tmp_path: Path) -> None:
    config = worker_config(tmp_path)
    assert dev_tool_loop(config).loop.builder.objects is context_objects(config)


# --- Planner 给 tool_loop 的 Goal Node 加上 context_read ---


def goal_nodes(stage: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in stage["nodes"] if n.get("kind") == "goal"]


def invalid_twice() -> FakeGateway:
    """两次规划都不合格：Planner 回退到保守规划（一个 tool_loop 的 Goal Node）。"""
    return FakeGateway(triage=signals(confidence=0.6), plan=["不是 JSON", "还不是"])


async def test_tool_loop_goal_nodes_get_context_read(tmp_path: Path) -> None:
    activities = PlannerActivities(Planner(invalid_twice()), StaticContextSource(context()), tools=ToolCatalog(dev_tools(worker_config(tmp_path))))
    out = await ActivityEnvironment().run(activities.plan_round, plan_input())
    [goal] = goal_nodes(out.stage)
    assert goal["mode"] == "tool_loop" and goal["tools"].count(CONTEXT_READ) == 1
    assert_valid("stage", out.stage)


async def test_model_planned_goal_nodes_keep_their_tools_and_get_context_read(tmp_path: Path) -> None:
    investigate = {
        "id": "investigate",
        "kind": "goal",
        "goal": "查退款率上升的原因",
        "done_when": "找到有数据支撑的原因",
        "mode": "tool_loop",
        "tools": ["web_search", "repo_read"],
        "effect_ceiling": "read",
        "output_schema": {"type": "object"},
    }
    plan = {
        "task_plan": {"triage": {"level": "full", "task_family": "analytics.attribution", "confidence": 0.7}, "horizon": "per_stage", "steps": PLAN_STEPS},
        "stage": {"seq": 1, "goal": "查原因", "exit_when": "找到原因", "nodes": [investigate]},
    }
    gateway = FakeGateway(triage=signals(confidence=0.6), plan=[plan])
    activities = PlannerActivities(Planner(gateway), StaticContextSource(context()), tools=ToolCatalog(dev_tools(worker_config(tmp_path))))
    out = await ActivityEnvironment().run(activities.plan_round, plan_input())
    [goal] = goal_nodes(out.stage)
    assert goal["id"] == "investigate", "模型的规划通过了校验，不是回退"
    assert goal["tools"] == ["web_search", "repo_read", CONTEXT_READ], "原有的工具不变，context_read 追加在后面"


async def test_context_read_is_not_added_when_read_is_not_allowed_or_not_registered(tmp_path: Path) -> None:
    registered = ToolCatalog(dev_tools(worker_config(tmp_path)))
    cases = [
        ("Task 的 effect 上限是 none", StaticContextSource(context(risk="none", ceiling="none")), registered),
        ("没登记 context_read", StaticContextSource(context()), ToolCatalog(LocalToolDriverProvider())),
        ("没给工具目录", StaticContextSource(context()), None),
    ]
    for why, source, tools in cases:
        out = await ActivityEnvironment().run(PlannerActivities(Planner(invalid_twice()), source, tools=tools).plan_round, plan_input())
        assert all(CONTEXT_READ not in (g.get("tools") or []) for g in goal_nodes(out.stage)), why
