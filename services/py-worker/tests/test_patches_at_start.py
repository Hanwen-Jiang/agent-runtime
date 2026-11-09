"""S-04 缺陷修复（D2、D3）：新的运行在第一个工作流任务里一次问完全部版本分支（见 workflows/patches.py）。

- 静态：工作流代码里每个 workflow.patched() 的 id 都是 workflows/patches.py 里的常量，并且在 AT_START 里；
  版本分支的 id 只在这个模块里定义；AT_START 只能在末尾追加（已有的顺序钉在这里）。
- 运行：Task 工作流、SubAgentWorkflow、SubgraphWorkflow 的新历史，第一个工作流任务里先是 PATCHES_AT_START，
  再按 AT_START 的顺序问完全部版本分支，之后再没有版本分支标记——local activity 之后不会第一次问版本分支。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agentruntime.common.v1 import enums_pb2

import agentruntime_worker.workflows as workflows_pkg
from agentruntime_worker import names
from agentruntime_worker.activities.types import StagePolicy, TemplateExpansion, ToolLoopStep
from agentruntime_worker.workflows import patches

from .history_split import first_task_patches
from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .test_stage_runner import atomic, deliver, goal, one_round, scripted_steps

WORKFLOW_DIR = Path(workflows_pkg.__file__).parent
# AT_START 已有的顺序：只能在末尾追加，改顺序或删掉会改变新历史开头的命令序列。
KNOWN_AT_START = (
    "s10-reviewer",
    "t09-policy-engine",
    "t11b-progress-events",
    "s04-stage-runner",
    "s05-human-channel",
    "d16-expect-reply-before-ask",
    "s05-reconcile",
    "d16-tool-loop-approval",
    "r01-audit-and-budget",
    "progress-node-and-tool",
    "tmp-time-tool-budgets",
    "tmp-decision-retry",
    "tmp-message-codes",
    "parallel-tool-calls",
    "context-usage-events",
    "guidance-and-interrupt",
    "interrupt-withdraws-question",
    "n01-references",
    "q87-context-builder-v2",
    "n04-task-plan",
    "n16-sub-agents",
    "n16r-sub-agent-wrap-up",
    "workspace-async-result-v1",
    "file-operation-terminal-v1",
    "child-file-whole-sheet-v1",
)


def patched_calls() -> list[tuple[str, int, ast.expr | None]]:
    """workflows/ 下（patches.py 之外）每个 workflow.patched() 调用：(文件, 行号, 参数)。"""
    out = []
    for path in sorted(WORKFLOW_DIR.glob("*.py")):
        if path.name == "patches.py":
            continue
        for node in ast.walk(ast.parse(path.read_text("utf-8"), path.name)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("patched", "deprecate_patch")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "workflow"
            ):
                out.append((path.name, node.lineno, node.args[0] if node.args else None))
    return out


def test_every_version_branch_is_asked_at_start() -> None:
    calls = patched_calls()
    assert calls, "workflows/ 里应当有版本分支"
    problems = []
    used: set[str] = set()
    for filename, line, arg in calls:
        value = getattr(patches, arg.id, None) if isinstance(arg, ast.Name) else None
        if not isinstance(value, str):
            problems.append(f"{filename}:{line}: workflow.patched 的参数要是 workflows/patches.py 里的常量")
        elif value not in patches.AT_START:
            problems.append(f"{filename}:{line}: {arg.id}（{value}）不在 patches.AT_START 里，新的运行不会在开头问它")
        else:
            used.add(value)
    assert not problems, "\n".join(problems)
    assert patches.PATCHES_AT_START not in used, "PATCHES_AT_START 只由 patches.at_start() 问"


def test_version_branch_ids_live_in_one_module() -> None:
    problems = []
    for path in sorted(WORKFLOW_DIR.glob("*.py")):
        if path.name == "patches.py":
            continue
        for node in ast.parse(path.read_text("utf-8"), path.name).body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.endswith("_PATCH"):
                        problems.append(f"{path.name}:{node.lineno}: 版本分支 {target.id} 要定义在 workflows/patches.py")
    assert not problems, "\n".join(problems)
    assert len(set(patches.AT_START)) == len(patches.AT_START), "AT_START 里不能有重复"
    assert patches.PATCHES_AT_START not in patches.AT_START


def test_at_start_is_only_appended_to() -> None:
    assert tuple(patches.AT_START[: len(KNOWN_AT_START)]) == KNOWN_AT_START, (
        "AT_START 只能在末尾追加：改顺序或删掉会改变新历史开头的命令序列；追加之后把新的 id 也加进 KNOWN_AT_START"
    )


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_new_runs_ask_every_version_branch_in_their_first_workflow_task(harness: StageHarness) -> None:
    s = harness.script
    s.policy = StagePolicy(max_parallel=4, max_inline_nodes=4, batch_size=3, batch_parallel=2)
    s.tools.update({"web_search": "read"})
    s.tool_steps["research"] = scripted_steps(ToolLoopStep(action="tool", tool="web_search", args={"q": "退款率"}), ToolLoopStep(action="finish", result={"ok": True}))
    s.recipes["triage@v1"] = TemplateExpansion(per_item=[atomic("classify", inputs={"ticket": "$item.id"})], items=[{"id": f"T-{i}"} for i in range(1, 6)], collect="classify")
    one_round(harness, goal("research", "sub_agent", tools=["web_search"]), goal("triage", "template", recipe="triage@v1"), deliver(after=["research", "triage"]))
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE

    sub_agent = sorted({c.workflow_id for c in s.calls if c.activity == names.TOOL_LOOP_STEP and c.workflow_id != inp.task_id})
    subgraphs = sorted({c.workflow_id for c in s.executed(inp.task_id) if c.node_id.startswith("triage.")})
    assert len(sub_agent) == 1 and len(subgraphs) == 2, (sub_agent, subgraphs)
    expected = [patches.PATCHES_AT_START, *patches.AT_START]
    for workflow_id in (inp.task_id, *sub_agent, *subgraphs):
        history = await harness.client.get_workflow_handle(workflow_id).fetch_history()
        first, later = first_task_patches(list(history.events))
        assert first == expected, f"{workflow_id} 的第一个工作流任务应按 AT_START 问完全部版本分支：{first}"
        assert not later, f"{workflow_id} 在第一个工作流任务之后又第一次问了版本分支：{later}"
