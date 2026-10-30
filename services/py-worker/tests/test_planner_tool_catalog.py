"""T-13（docs/plan/live-e2e.md 第 2 节“已知要补”）：真 Planner 的 Node Catalog 带上 ToolDriver 登记的工具。

现在 dev_planner 的静态上下文与 Registry 上下文里 catalog.tools 都是空的：提示里不列工具，validate 又按 catalog.tools
校验 Goal Node 的 tools，真 Planner 规划不出用平台内工具的 tool_loop。工具与 effect 要取自 ToolDriver 的登记
（T-10 的 ToolCatalog，同名取最大版本），按 Task 的租户查；模板照旧（静态的两个，或 Registry 按固定版本）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.planner.activities import dev_planner
from agentruntime_worker.planner.validate import problems
from agentruntime_worker.registry import GrpcRegistry, RegistryContextSource

from .test_registry import plan_input, seeded, serve

PLATFORM_TOOLS = {
    "context_read": "read",
    "repo_read": "read",
    "repo_list": "read",
    "repo_grep": "read",
    "repo_edit": "write_reversible",
    "shell_run": "write_reversible",
}


def config(tmp_path: Path, **overrides: Any) -> WorkerConfig:
    values: dict[str, Any] = {
        "planner": "model",
        "data_dir": tmp_path,
        "object_store_dir": tmp_path / "objects",
        "tool_workspace_root": tmp_path / "workspaces",
    }
    values.update(overrides)
    return WorkerConfig(**values)


def ci_fix_stage() -> dict[str, Any]:
    return {
        "seq": 1,
        "goal": "修好让 CI 失败的代码",
        "exit_when": "unittest 全部通过",
        "nodes": [
            {
                "id": "fix",
                "kind": "goal",
                "goal": "找出失败的用例并修好 pricing.py",
                "done_when": "python -m unittest 全部通过，测试文件没有改动",
                "mode": "tool_loop",
                "tools": ["repo_list", "repo_read", "repo_grep", "repo_edit", "shell_run"],
                "effect_ceiling": "write_reversible",
                "output_schema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
            }
        ],
    }


async def test_the_static_planning_context_lists_the_registered_tools(tmp_path: Path) -> None:
    context = await dev_planner(config(tmp_path)).contexts.load(plan_input())
    assert context.catalog.tools == PLATFORM_TOOLS, "工具与 effect 取自 ToolDriver 的登记"
    assert context.catalog.templates == {"answer@v1": ("model", "none"), "rewrite@v1": ("model", "none")}, "模板不变"


async def test_without_a_workspace_only_context_read_is_listed(tmp_path: Path) -> None:
    context = await dev_planner(config(tmp_path, tool_workspace_root=None)).contexts.load(plan_input())
    assert context.catalog.tools == {"context_read": "read"}, "没配工作区就只有总是登记的 context_read"


async def test_the_registry_planning_context_lists_the_registered_tools(tmp_path: Path) -> None:
    fake = seeded()
    async with serve(fake) as address:
        planner = dev_planner(config(tmp_path, registry_address=address))
        # 外层是 Session 的对话（TranscriptContextSource），里面是 ToolsContextSource，再里面才是 Registry。
        registry_source = planner.contexts.inner.inner
        assert isinstance(registry_source, RegistryContextSource)
        context = await planner.contexts.load(plan_input())
        await registry_source.registry.aclose()
    assert context.catalog.tools == PLATFORM_TOOLS
    assert "sql_query@v1" in context.catalog.templates, "模板照旧按 Task 固定的版本取自 Registry"


async def test_a_tool_loop_over_platform_tools_passes_planner_validation(tmp_path: Path) -> None:
    planner = dev_planner(config(tmp_path))
    context = await planner.contexts.load(plan_input({"constraints": {"effect_ceiling": "write_reversible"}}))
    assert problems(ci_fix_stage(), None, level="fast", context=context, keep_nodes=[]) == []


async def test_a_tool_above_the_goals_ceiling_is_still_rejected(tmp_path: Path) -> None:
    planner = dev_planner(config(tmp_path))
    context = await planner.contexts.load(plan_input({"constraints": {"effect_ceiling": "write_reversible"}}))
    stage = ci_fix_stage()
    stage["nodes"][0]["effect_ceiling"] = "read"
    found = problems(stage, None, level="fast", context=context, keep_nodes=[])
    assert any("repo_edit" in p and "shell_run" in p for p in found), found


async def test_an_unregistered_tool_is_still_rejected(tmp_path: Path) -> None:
    planner = dev_planner(config(tmp_path))
    context = await planner.contexts.load(plan_input({"constraints": {"effect_ceiling": "write_reversible"}}))
    stage = ci_fix_stage()
    stage["nodes"][0]["tools"].append("deploy_prod")
    found = problems(stage, None, level="fast", context=context, keep_nodes=[])
    assert any("deploy_prod" in p for p in found), found
