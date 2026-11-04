"""N16-R 第三段（docs/plan/sub-agent.md 第 6 节）：超份额时子 Agent 带着已有的结论收尾，不是失败。

1. 子 Agent 按份额（Node 的 budget.usd）停下时，不带工具再问一步：history 末尾一条说明“份额用完了，按要求的格式交回已有的结论”，
   模型交回的结果标 partial，这个 sub_agent 算成功；这一步的花费照记。
2. 模型这一步还要调工具，或这一步失败：照旧 denied（budget_exhausted）。
3. 只对份额生效：Task 的预算用完照旧 denied，不再多花一步。
4. 版本分支 SUB_AGENT_WRAP_UP_PATCH 追加在 AT_START 末尾；之前的历史（没有它）照旧 denied，golden 历史照常回放（test_stage_replay 等）。
"""

from __future__ import annotations

from typing import Any

import pytest

from agentruntime.workflow.v1 import task_pb2
from agentruntime_worker import names
from agentruntime_worker.activities.types import ToolLoopStep
from agentruntime_worker.workflows.patches import AT_START, N16_PATCH, SUB_AGENT_WRAP_UP_PATCH

from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .test_stage_runner import deliver, goal, one_round

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

FINDING = {"cards": [{"claim": "A 支持 30 天退款", "evidence": "政策页", "source": "https://a.example", "confidence": 0.7}]}


def agent(h: StageHarness, nid: str, *, wrap_up: Any = None, cost: float = 0.02) -> None:
    """前两步各调一次工具、各花 cost；份额用完之后（没有工具的那一步）交回 wrap_up（默认交回 FINDING）。"""
    h.script.tools["repo_grep"] = "read"
    h.script.tool_results["repo_grep"] = lambda i: {"matches": ["x"]}

    def step(inp: Any) -> ToolLoopStep:
        if not inp.node.get("tools"):
            return wrap_up if wrap_up is not None else ToolLoopStep(action="finish", result=dict(FINDING), cost_usd=0.001)
        return ToolLoopStep(action="tool", tool="repo_grep", args={"pattern": nid, "path": f"p{inp.iteration}"}, cost_usd=cost)

    h.script.tool_steps[nid] = step


def steps_of(h: StageHarness, nid: str) -> list[Any]:
    return [c.inp for c in h.script.calls if c.activity == names.TOOL_LOOP_STEP and c.workflow_id.endswith(nid)]


def test_the_patch_is_appended_at_the_end() -> None:
    assert AT_START[-1] == SUB_AGENT_WRAP_UP_PATCH and AT_START.index(N16_PATCH) == len(AT_START) - 2


async def test_a_sub_agent_out_of_its_share_wraps_up_with_partial_results(harness: StageHarness) -> None:  # noqa: F811
    agent(harness, "scan")
    one_round(harness, goal("scan", "sub_agent", tools=["repo_grep"], budget={"usd": 0.03}), deliver(after=["scan"]))
    inp, _ = await harness.run(budget=task_pb2.Budget(usd_micros=5_000_000))
    result = harness.script.review(inp.task_id, 1).stage_results["scan"]
    assert result.status == "succeeded" and result.result["partial"] is True and result.result["cards"] == FINDING["cards"]
    last = steps_of(harness, "scan")[-1]
    assert last.node.get("tools") == [] and "份额" in last.history[-1]["content"], "收尾这一步不给工具，说明为什么停"
    assert len(steps_of(harness, "scan")) == 3, "两步用完份额，再加一步收尾"


async def test_a_wrap_up_that_still_calls_tools_is_denied(harness: StageHarness) -> None:  # noqa: F811
    agent(harness, "scan", wrap_up=ToolLoopStep(action="tool", tool="repo_grep", args={"pattern": "more", "path": "x"}, cost_usd=0.001))
    one_round(harness, goal("scan", "sub_agent", tools=["repo_grep"], budget={"usd": 0.03}), deliver(after=["scan"]), scenario="stubborn")
    inp, _ = await harness.run(budget=task_pb2.Budget(usd_micros=5_000_000), scenario="stubborn")
    result = harness.script.review(inp.task_id, 1).stage_results["scan"]
    assert result.failure_class == "denied" and result.error_code == "budget_exhausted"
    assert not [c for c in harness.script.calls if c.activity == names.CALL_TOOL and c.inp.args.get("pattern") == "more"], "收尾时不再调工具"


async def test_running_out_of_the_task_budget_is_still_denied(harness: StageHarness) -> None:  # noqa: F811
    agent(harness, "scan")
    one_round(harness, goal("scan", "sub_agent", tools=["repo_grep"]), deliver(after=["scan"]), scenario="task_budget")
    inp, _ = await harness.run(budget=task_pb2.Budget(usd_micros=30_000), scenario="task_budget")
    result = harness.script.review(inp.task_id, 1).stage_results["scan"]
    assert result.failure_class == "denied" and result.error_code == "budget_exhausted"
    assert all(s.node.get("tools") for s in steps_of(harness, "scan")), "Task 的预算用完不多花一步收尾"
