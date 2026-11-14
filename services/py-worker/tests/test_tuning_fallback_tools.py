"""TMP 计划第 11.15 节，调优第一轮：保守规划要给能干活的工具。

Planner 的结构化输出失败时回退到保守规划。它的 Goal Node 不写 tools，规划之后只被加上 context_read，读不了仓库，也改不了文件，只能空转
（下午那次 django-11433 的 12 轮里有 5 轮是它）。给它 Node Catalog 里 effect 上限以内的工具；不可逆的工具永远不给，那要人审批，不该由回退路径带上。
"""

from __future__ import annotations

from agentruntime_worker.planner.context import CatalogView, PlanningContext
from agentruntime_worker.planner.planner import Planner

from .planner_support import FakeGateway, request

CATALOG = CatalogView(
    templates={"answer@v1": ("model", "none")},
    tools={"repo_read": "read", "repo_grep": "read", "context_read": "read", "repo_edit": "write_reversible", "shell_run": "write_reversible", "request_network": "write_irreversible"},
)


def planned(ceiling: str, catalog: CatalogView = CATALOG) -> dict:
    ctx = PlanningContext(risk_authorized="read", effect_ceiling=ceiling, catalog=catalog, recipes=[])
    result = Planner(FakeGateway()).plan_without_model(request("修复这个 bug", ctx=ctx))
    assert result.fallback
    return result.stage


def tools_of(stage: dict) -> set[str]:
    # 第一个是探索的 Goal Node；目录里有回答模板时后面另有一个不带工具的 deliver（planner.py 的回退）。
    node = stage["nodes"][0]
    assert all(not n.get("tools") for n in stage["nodes"][1:])
    return set(node.get("tools") or [])


def test_a_write_capable_task_gets_the_tools_to_do_the_work() -> None:
    assert tools_of(planned("write_reversible")) == {"repo_read", "repo_grep", "context_read", "repo_edit", "shell_run"}


def test_a_read_only_task_gets_only_read_tools() -> None:
    assert tools_of(planned("read")) == {"repo_read", "repo_grep", "context_read"}


def test_irreversible_tools_are_never_handed_out_by_the_fallback() -> None:
    """即使 effect 上限允许：不可逆的调用要逐次审批，回退路径不该替模型选上它。"""
    assert "request_network" not in tools_of(planned("write_irreversible"))


def test_the_order_is_stable() -> None:
    """工作流代码要确定：不依赖 dict 的遍历顺序。"""
    node = planned("write_reversible")["nodes"][0]  # 探索的 Goal Node；后面可能另有 deliver
    assert node["tools"] == sorted(node["tools"])


def test_a_catalog_without_tools_still_gives_a_valid_stage() -> None:
    stage = planned("write_reversible", CatalogView(templates={"answer@v1": ("model", "none")}, tools={}))
    assert stage["nodes"][0]["mode"] == "tool_loop"
    assert tools_of(stage) == set()


def test_the_write_capable_node_also_gets_a_write_effect_ceiling_that_validates() -> None:
    """工具的 effect 不能高于节点的上限，否则校验会拒绝这份规划。"""
    node = planned("write_reversible")["nodes"][0]  # 探索的 Goal Node；后面可能另有 deliver
    assert node.get("effect_ceiling", "write_reversible") == "write_reversible"
