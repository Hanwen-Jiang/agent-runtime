"""R-03 评审的本期修（docs/plan/review-R-03-context-builder.md 第 4 节）：

1. Goal Node 的输入超过 offload_tokens 时外置：写进 ObjectStore，brief 里只留路径与摘录（设计书 3.2.7 第 2 条、G.1）。
2. 精确状态不受 status 的 500 token 上限截断（3.2.7 第 4 条）：status 只放进度、最近的动作与错误、下一步；精确状态单独成段，
   放在 history 之后、status 之前；单独超过它的上限（默认 1500 token）时，超出的条目外置，注明条数与路径。
3. 阈值的 token 计数算上随请求给出的工具定义（3.2.7 第 3 条）。
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

from agentruntime_worker.context.builder import ContextBuilder, message_tokens, tenant_context
from agentruntime_worker.context.partitions import BRIEF_HEADER, STATUS_HEADER
from agentruntime_worker.context.tokens import estimate

from .context_support import (
    GOAL_NODE,
    TASK_ID,
    TASK_STATE,
    TENANT,
    ContextGateway,
    config,
    error_step,
    filler,
    history,
    objects,
    read_step,
    request,
    text_of,
)

PRECISE_STATE_TOKENS = 1_500


def big_state(n: int) -> dict[str, Any]:
    return {**TASK_STATE, "facts": [f"已核对：模块 m{i} 的配置项 key_{i} 取值 {1000 + i}" for i in range(n)]}


# --- 1. Goal Node 的大输入外置 ---


async def test_large_goal_inputs_are_offloaded_from_the_brief(tmp_path: Path) -> None:
    upstream = {"report": filler(99, 30_000), "region": "east"}
    node = {**GOAL_NODE, "inputs": upstream}
    provider = objects(tmp_path)
    b = ContextBuilder(ContextGateway(), provider, config(effective_tokens=100_000, offload_tokens=2_000))
    req = dataclasses.replace(request([read_step(0)]), node=node)
    built = await b.build(req)
    brief = next(m.text for m in built.messages if m.text.startswith(BRIEF_HEADER))
    assert upstream["report"] not in text_of(built.messages), "大输入不原样进上下文"
    assert upstream["report"][:100] in brief, "brief 里留开头的摘录"
    store = provider.bind(tenant_context(TENANT))
    key = f"context/{TASK_ID}/r1/fix/inputs.json"
    assert store.uri(key) in brief, "brief 里给出完整输入的路径"
    assert json.loads((await store.get(key)).data) == upstream
    assert estimate(brief) < 1_000, "brief 保持在固定的小体量"
    again = await b.build(req)
    assert text_of(again.messages) == text_of(built.messages), "重复构建结果相同（键固定、内容不变）"


async def test_small_goal_inputs_stay_inline(tmp_path: Path) -> None:
    node = {**GOAL_NODE, "inputs": {"region": "east", "ticket": "PR-482"}}
    built = await ContextBuilder(ContextGateway(), objects(tmp_path), config(effective_tokens=100_000)).build(
        dataclasses.replace(request([read_step(0)]), node=node)
    )
    brief = next(m.text for m in built.messages if m.text.startswith(BRIEF_HEADER))
    assert '"region": "east"' in brief and "PR-482" in brief and "obj://" not in brief


# --- 2. 精确状态不被 status 的上限截掉 ---


async def test_precise_state_is_not_cut_by_the_status_limit(tmp_path: Path) -> None:
    state = big_state(40)  # 约 1000 token：超过 status 的 500，不到精确状态的上限
    steps = [*history(3), error_step(3)]
    built = await ContextBuilder(ContextGateway(), objects(tmp_path), config(effective_tokens=100_000)).build(
        dataclasses.replace(request(steps), task_state=state)
    )
    text = text_of(built.messages)
    for fact in state["facts"]:
        assert fact in text, f"精确状态逐条都在上下文里：{fact}"
    for line in ("effect 上限：write_reversible", "待查：fixtures/orders.json 是否需要更新", "产物：obj://t1/artifacts/patch-checkout.diff"):
        assert line in text

    status = built.messages[-1].text
    assert status.startswith(STATUS_HEADER) and estimate(status) <= 500, "status 仍在最后、不超过 500 token"
    assert "最近的动作" in status and "最近的错误" in status and "patch 第 3 处不适用" in status and "下一步" in status, (
        "最近的动作、错误与下一步不因精确状态多而被截掉"
    )

    at = next(i for i, m in enumerate(built.messages) if state["facts"][0] in m.text)
    last_history = max(i for i, m in enumerate(built.messages) if m.role == mg.CHAT_ROLE_TOOL)
    assert last_history < at < len(built.messages) - 1, "精确状态单独成段：在 history 之后、status 之前，不动缓存前缀"


async def test_precise_state_beyond_its_budget_is_offloaded_with_a_count(tmp_path: Path) -> None:
    state = big_state(200)  # 约 5000 token
    provider = objects(tmp_path)
    built = await ContextBuilder(ContextGateway(), provider, config(effective_tokens=100_000)).build(
        dataclasses.replace(request(history(3)), task_state=state)
    )
    text = text_of(built.messages)
    block = next(m.text for m in built.messages if state["facts"][0] in m.text)
    assert estimate(block) <= PRECISE_STATE_TOKENS + 50, "精确状态这一段有上限"
    assert state["facts"][-1] not in text
    omitted = re.search(r"另有 (\d+) 条", block)
    assert omitted and int(omitted.group(1)) > 0, "注明还有多少条没放进来"
    store = provider.bind(tenant_context(TENANT))
    key = f"context/{TASK_ID}/r1/fix/task-state.json"
    assert store.uri(key) in block, "给出完整精确状态的路径"
    assert json.loads((await store.get(key)).data)["facts"] == state["facts"]


# --- 3. 工具定义计入阈值 ---


def tool_specs(n: int, chars: int) -> list[mg.ToolSpec]:
    return [mg.ToolSpec(name=f"tool_{i}", description=filler(i, chars)) for i in range(n)]


async def test_tool_definitions_count_toward_the_thresholds(tmp_path: Path) -> None:
    steps = history(4, chars=600)
    plain = await ContextBuilder(ContextGateway(), objects(tmp_path / "a"), config(effective_tokens=4_000)).build(request(steps))
    assert plain.applied == [], "不算工具定义时低于 mask_at"
    tools = tool_specs(2, 2_000)  # 约 1000 token
    built = await ContextBuilder(ContextGateway(), objects(tmp_path / "b"), config(effective_tokens=4_000)).build(request(steps, tools=tools))
    assert built.applied == ["mask"], "算上工具定义就超过 mask_at"
    assert built.tokens >= message_tokens(built.messages) + 900, "BuiltContext.tokens 含工具定义"
